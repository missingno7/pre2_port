"""The consolidated renderer seam ``render_frame`` (pre2.recovered.render_frame).

These tests guard the *composition*: that ``render_frame`` runs the recovered leaves in
the original per-frame order (palette fade -> animated-grid -> grid -> scroll) and routes
the right ``RendererState`` fields to each. The leaves themselves are byte-exact vs the
ASM under their own tests + in-VM lockstep; the standalone byte-exact composition (the
renderer-owned bg buffer, reproduced with no VM stepping) is validated in-VM on snapshots
212037/185902 (0 divergence incl. grid-redraw frames) — see docs/pre2/renderer_status.md.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pre2.recovered.render_frame as RF  # noqa: E402
from pre2.recovered.render_frame import FadeStep, RendererState, render_frame  # noqa: E402


def _state(**over):
    base = dict(
        tiles=bytes(0x100), type_tbl=bytes(256), flag_tbl=bytes(256),
        blit_type=bytes(256), mask_region=bytes(0x2000), anim_xlat=bytes(256),
        camera_x=3, camera_y=4, prev_x=3, prev_y=4, col_ring=3, fine_scroll=0,
        row_ring=4, scroll_src=0x4000, dest_page=0x2000, row_factor=1,
        dirty=0, dirty_rows=0, fade=None,
    )
    base.update(over)
    return RendererState(**base)


def _shim(monkeypatch_calls):
    """Replace the four leaves with recorders; return a restore() callable."""
    saved = (RF.fade_palette, RF.redraw_animated_grid, RF.draw_grid, RF.scroll_copy)

    class _Grid:
        redrew = False

    def fade(a, b, amt):
        monkeypatch_calls.append(("fade", a, b, amt))
        return bytes(48), True

    RF.fade_palette = fade
    RF.redraw_animated_grid = lambda *a, **k: monkeypatch_calls.append(("animgrid", a))
    RF.draw_grid = lambda *a, **k: (monkeypatch_calls.append(("grid", a)), _Grid())[1]
    RF.scroll_copy = lambda *a, **k: monkeypatch_calls.append(("scroll", a))

    def restore():
        RF.fade_palette, RF.redraw_animated_grid, RF.draw_grid, RF.scroll_copy = saved

    return restore


def test_render_frame_composition_order_no_fade():
    calls: list = []
    restore = _shim(calls)
    try:
        render_frame(_state(), [None] * 4, dac=None)
    finally:
        restore()
    assert [c[0] for c in calls] == ["animgrid", "grid", "scroll"]


def test_render_frame_runs_fade_first_and_updates_dac():
    calls: list = []
    restore = _shim(calls)
    dac = [[9, 9, 9] for _ in range(16)]
    try:
        render_frame(_state(fade=FadeStep(bytes(48), bytes(48), 1)), [None] * 4, dac=dac)
    finally:
        restore()
    assert [c[0] for c in calls] == ["fade", "animgrid", "grid", "scroll"]
    # fade shim returned all-zero 6-bit components -> every DAC colour cleared to 0
    assert dac == [[0, 0, 0] for _ in range(16)]


def test_render_frame_runs_sprite_pass_when_object_camera_set():
    """With object_camera set, render_frame runs the moving-sprite pass (plan_frame ->
    paint_sprite) after scroll; with it None, the pass is skipped."""
    import types

    class _Draw:
        src_seg = 7
        src_off = 0
        src_bw = 1
        full_rows = 1

    cam = types.SimpleNamespace(row_stride=40)

    # with object_camera: plan_frame yields one draw -> one paint_sprite
    saved = (RF.fade_palette, RF.redraw_animated_grid, RF.draw_grid, RF.scroll_copy,
             RF.plan_frame, RF.paint_sprite)
    calls: list = []

    class _Grid:
        redrew = False

    RF.fade_palette = lambda a, b, amt: (bytes(48), True)
    RF.redraw_animated_grid = lambda *a, **k: calls.append("animgrid")
    RF.draw_grid = lambda *a, **k: (calls.append("grid"), _Grid())[1]
    RF.scroll_copy = lambda *a, **k: calls.append("scroll")
    RF.plan_frame = lambda sprites, attrs, c: (calls.append("plan"), [_Draw()])[1]
    RF.paint_sprite = lambda planes, d, src, stride: calls.append(("paint", stride))
    try:
        render_frame(_state(object_camera=cam, object_src_banks={7: bytes(128)}),
                     [None] * 4, dac=None)
    finally:
        (RF.fade_palette, RF.redraw_animated_grid, RF.draw_grid, RF.scroll_copy,
         RF.plan_frame, RF.paint_sprite) = saved
    assert calls == ["animgrid", "grid", "scroll", "plan", ("paint", 40)]


def test_render_frame_routes_state_to_leaves():
    calls: list = []
    restore = _shim(calls)
    st = _state(scroll_src=0x4123, dest_page=0x2000, camera_x=0x107)
    try:
        render_frame(st, [None] * 4, dac=None)
    finally:
        restore()
    by = {c[0]: c[1] for c in calls}
    # animated-grid gets camera_x as a byte (low 8 bits) + scroll_src as the dest
    ag = by["animgrid"]
    assert ag[6] == (0x107 & 0xFF) and ag[9] == 0x4123
    # scroll-copy gets scroll_src + dest_page
    assert by["scroll"][1] == 0x4123 and by["scroll"][2] == 0x2000
