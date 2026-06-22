"""Tests for the non-gameplay scene seam: SceneState -> render_scene.

Synthetic fixtures. Proves the seam COMPOSES the recovered leaves (background present + text
via draw_string + palette via fade_palette) in z-order. The byte-exact fidelity of draw_string
itself vs the VM/VGA oracle is verified separately by ``pre2/probes/capture_text_draw.py`` once
a mid-draw witness exists.
"""
from __future__ import annotations

from pre2.recovered.scene import (
    FadeStep, MenuHighlight, SceneImage, SceneState, TextRun, render_scene, SCENE_TITLE,
)

PLANE = 0x2000


def _planes():
    return [bytearray(0x10000) for _ in range(4)]


def _scene_with_text():
    # a font where every byte is 0xAB, so a drawn glyph cell is a recognisable non-zero value
    font = bytes([0xAB]) * 0x2000
    bg = SceneImage(planes=(bytes([0x11]) * PLANE, bytes([0x22]) * PLANE,
                            bytes([0x33]) * PLANE, bytes([0x44]) * PLANE))
    run = TextRun(text=b"A1Z", font_base=0, pen=0, advance=4, page_draw=0, page_clear=0)
    return SceneState(scene_id=SCENE_TITLE, phase="title", background=bg, font=font,
                      text_runs=(run,))


def test_render_scene_composes_background_then_text():
    planes = _planes()
    render_scene(_scene_with_text(), planes)
    # background present: the four planes carry the picture (provisional planar copy)
    assert planes[0][0] == 0x11 and planes[1][0] == 0x22
    # text drew on top, into planes 2 and 3 ONLY (draw_string's 2bpp font), at the pen cell
    # (first char: pen=4 -> base=0x54). Background plane 2/3 there is overwritten by the glyph.
    assert planes[2][0x54] == 0xAB and planes[3][0x54] == 0xAB
    # planes 0 and 1 at the text cell are still the background (text never touches them)
    assert planes[0][0x54] == 0x11 and planes[1][0x54] == 0x22


def test_render_scene_applies_palette_fade_to_dac():
    dac = [[0, 0, 0] for _ in range(16)]
    fade = FadeStep(a=bytes(48), b=bytes([0x3F]) * 48, amount=0x20)
    render_scene(SceneState(scene_id=SCENE_TITLE, fade=fade), _planes(), dac)
    # fade steps the DAC from a (all 0) toward b (all 0x3F): every entry moved off zero
    assert all(0 < c[0] <= 0x3F for c in dac)


def test_render_scene_static_palette():
    dac = [[0, 0, 0] for _ in range(16)]
    pal = tuple([i, i, i] for i in range(16))
    render_scene(SceneState(palette=pal), _planes(), dac)
    assert dac[5] == [5, 5, 5] and dac[15] == [15, 15, 15]


def test_render_scene_empty_is_noop():
    planes = _planes()
    render_scene(SceneState(), planes)            # no background/text/palette
    assert not any(any(p) for p in planes)        # nothing drawn


def test_cursor_leaf_is_wired_but_noop():
    # the cursor leaf is in the composition (call site exists to merge into) but un-recovered
    planes = _planes()
    render_scene(SceneState(cursor=MenuHighlight(selection=2, item_count=4)), planes)
    assert not any(any(p) for p in planes)        # draw_cursor is a no-op until recovered
