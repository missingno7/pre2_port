"""Pure-logic tests for the firefly-swarm draw (pre2.recovered.fireflies.draw_fireflies).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_fireflies.py, Δ=0 on the firefly
snapshot 140330); these lock the recovered plot mechanics in the suite with hand-computed cases: the
camera-relative >>3 fixed-point screen position, the unsigned off-screen culls, and the OR-into-all-four-
planes single-pixel plot (dos_re has no Set/Reset emulation, so every firefly is color 15)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.fireflies import draw_fireflies, firefly_color  # noqa: E402


def _planes():
    return [bytearray(0x10000) for _ in range(4)]


def test_plot_all_four_planes_no_camera():
    # x=200 (>>3 = 25), y=80 (>>3 = 10); no camera -> screen (25, 10)
    planes = _planes()
    draw_fireflies(planes, [(200, 80, 0)], cam_col=0, cam_row=0, page=0)
    sx, sy = 25, 10
    off = sy * 0x28 + (sx >> 3)
    bit = 0x80 >> (sx & 7)
    for p in range(4):                      # color 15: all four planes (Set/Reset not emulated)
        assert planes[p][off] == bit
    assert sum(sum(pl) for pl in planes) == bit * 4


def test_timer_parity_does_not_change_faithful_draw():
    # even and odd timers both draw all four planes under the VM oracle
    for timer in (0, 1, 2, 7):
        planes = _planes()
        draw_fireflies(planes, [(200, 80, timer)], 0, 0, 0)
        off = 10 * 0x28 + (25 >> 3)
        bit = 0x80 >> (25 & 7)
        for p in range(4):
            assert planes[p][off] == bit


def test_camera_and_page_offset():
    # cam (col=2, row=1) -> cam_x=32, cam_y=16. x=320 (>>3=40), y=160 (>>3=20)
    planes = _planes()
    draw_fireflies(planes, [(320, 160, 0)], cam_col=2, cam_row=1, page=0x2000)
    sx = (320 >> 3) - 32                     # 40 - 32 = 8
    sy = (160 >> 3) - 16                     # 20 - 16 = 4
    off = (0x2000 + sy * 0x28 + (sx >> 3)) & 0xFFFF
    assert planes[0][off] == (0x80 >> (sx & 7))


def test_offscreen_culls():
    # sx >= 0x140 culled
    planes = _planes()
    draw_fireflies(planes, [(0x140 << 3, 80, 0)], 0, 0, 0)
    assert sum(sum(pl) for pl in planes) == 0
    # sy >= 0xB0 culled
    planes = _planes()
    draw_fireflies(planes, [(80, 0xB0 << 3, 0)], 0, 0, 0)
    assert sum(sum(pl) for pl in planes) == 0
    # negative screen pos (camera past the firefly) culls as unsigned-large
    planes = _planes()
    draw_fireflies(planes, [(80, 80, 0)], cam_col=10, cam_row=0, page=0)  # sx = 10 - 160 < 0
    assert sum(sum(pl) for pl in planes) == 0


def test_arithmetic_shift_negative_world_pos():
    # negative world Y: sar keeps the sign. y = -8 -> y>>3 = -1 -> off-screen-top cull
    planes = _planes()
    draw_fireflies(planes, [(200, -8, 0)], cam_col=0, cam_row=0, page=0)
    assert sum(sum(pl) for pl in planes) == 0


def test_firefly_color_helper_flicker():
    # the real-hardware color (for the enhanced renderer): even=14, odd=15
    assert firefly_color(0) == 14
    assert firefly_color(2) == 14
    assert firefly_color(1) == 15
    assert firefly_color(7) == 15
