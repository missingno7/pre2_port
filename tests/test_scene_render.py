"""Tests for the non-gameplay scene seam: SceneState -> render_scene(state, target).

Synthetic fixtures. Proves the seam COMPOSES the recovered leaves (background present + text
via draw_string + palette via fade_palette) in z-order, for both the planar (0Dh) and linear
(13h) scene modes. draw_string's own byte-exact fidelity vs the VM/VGA oracle is confirmed by
disassembly (pre2/recovered/text.py) and the lockstep harness pre2/probes/capture_text_draw.py.
"""
from __future__ import annotations

from pre2.recovered.scene import (
    FadeStep, MenuHighlight, RenderTarget, SceneImage, SceneState, TextRun, render_scene,
    MODE_LINEAR, MODE_PLANAR, SCENE_TITLE,
)

PLANE = 0x2000


def _planar_target():
    return RenderTarget(planes=[bytearray(0x10000) for _ in range(4)],
                        dac=[[0, 0, 0] for _ in range(16)])


def _planar_scene_with_text():
    font = bytes([0xAB]) * 0x2000   # every byte 0xAB -> a drawn glyph cell is recognisable
    bg = SceneImage(planes=(bytes([0x11]) * PLANE, bytes([0x22]) * PLANE,
                            bytes([0x33]) * PLANE, bytes([0x44]) * PLANE))
    run = TextRun(text=b"A1Z", font_base=0, pen=0, advance=4, page_draw=0, page_clear=0)
    return SceneState(scene_id=SCENE_TITLE, phase="menu", video_mode=MODE_PLANAR,
                      background=bg, font=font, text_runs=(run,))


def test_render_scene_planar_composes_background_then_text():
    t = _planar_target()
    render_scene(_planar_scene_with_text(), t)
    p = t.planes
    assert p[0][0] == 0x11 and p[1][0] == 0x22                 # background present
    assert p[2][0x54] == 0xAB and p[3][0x54] == 0xAB           # text on top, planes 2|3 only
    assert p[0][0x54] == 0x11 and p[1][0x54] == 0x22           # text never touches planes 0|1


def test_render_scene_linear_256_colour_image():
    pixels = bytes((i * 7) & 0xFF for i in range(320 * 200))
    state = SceneState(video_mode=MODE_LINEAR, scene_id=SCENE_TITLE,
                       background=SceneImage(pixels=pixels))
    t = RenderTarget(linear=bytearray(320 * 200), dac=[[0, 0, 0] for _ in range(256)])
    render_scene(state, t)
    assert bytes(t.linear) == pixels                            # the image is presented verbatim


def test_render_scene_applies_palette_fade_to_dac():
    t = _planar_target()
    fade = FadeStep(a=bytes(48), b=bytes([0x3F]) * 48, amount=0x20)
    render_scene(SceneState(video_mode=MODE_PLANAR, fade=fade), t)
    assert all(0 < c[0] <= 0x3F for c in t.dac)                 # fade stepped every DAC entry


def test_render_scene_static_palette_any_length():
    t = RenderTarget(dac=[[0, 0, 0] for _ in range(256)])
    pal = tuple([i & 0x3F, 0, 0] for i in range(256))
    render_scene(SceneState(video_mode=MODE_LINEAR, palette=pal), t)
    assert t.dac[5] == [5, 0, 0] and t.dac[200] == [200 & 0x3F, 0, 0]


def test_render_scene_empty_is_noop():
    t = _planar_target()
    render_scene(SceneState(), t)
    assert not any(any(p) for p in t.planes)


def test_cursor_leaf_is_wired_but_noop():
    t = _planar_target()
    render_scene(SceneState(video_mode=MODE_PLANAR, cursor=MenuHighlight(selection=2, item_count=4)), t)
    assert not any(any(p) for p in t.planes)                    # draw_cursor is a no-op until recovered
