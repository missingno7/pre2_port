"""Pure-logic tests for the point-particle draw (pre2.recovered.particles.draw_particles).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_particles.py, Δ=0 on the spider
snapshot); these lock the recovered formula + plot mechanics (velocity from the sin/cos tables, the
camera-relative screen position, the off-screen culls, and the OR-into-all-planes single-pixel plot)
in the suite with hand-computed cases."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.particles import draw_particles  # noqa: E402


def _planes():
    return [bytearray(0x10000) for _ in range(4)]


def test_plot_at_camera_relative_position_no_velocity():
    # zero tables -> no velocity; pixel lands at (x - cam_x, y - y_bias - cam_y)
    planes = _planes()
    zero = bytes(256)
    draw_particles(planes, [(100, 50, 0, 10)], cam_col=0, cam_row=0, y_bias=0,
                   page=0, cos_table=zero, sin_table=zero)
    off = 50 * 0x28 + (100 >> 3)        # row 50, byte col 12
    bit = 0x80 >> (100 & 7)             # bit for x%8 == 4 -> 0x08
    for p in range(4):
        assert planes[p][off] == bit    # set in ALL four planes (white)
    # nothing else touched
    assert sum(sum(pl) for pl in planes) == bit * 4


def test_velocity_from_tables_advances_position():
    # cos[10]=8, speed=16 -> vx = ((s8(8)>>2) * 16) >> 4 = (2*16)>>4 = 2
    planes = _planes()
    cos = bytearray(256)
    cos[10] = 8
    sin = bytes(256)
    draw_particles(planes, [(98, 30, 10, 16)], cam_col=0, cam_row=0, y_bias=0,
                   page=0, cos_table=bytes(cos), sin_table=sin)
    x = 98 + 2                           # advanced by vx=2
    off = 30 * 0x28 + (x >> 3)
    bit = 0x80 >> (x & 7)
    assert planes[0][off] == bit


def test_signed_velocity_and_y_bias():
    # cos[5] = 0xC4 (-60): vx = ((-60>>2) * speed8) >> 4; sin via table; y_bias shifts Y up
    planes = _planes()
    cos = bytearray(256)
    cos[5] = 0xC4                        # -60 -> >>2 = -15
    draw_particles(planes, [(200, 100, 5, 16)], cam_col=0, cam_row=0, y_bias=4,
                   page=0, cos_table=bytes(cos), sin_table=bytes(256))
    x = 200 + (((-15) * 16) >> 4)        # = 200 - 15 = 185
    y = 100 - 4                          # y_bias subtracted
    off = y * 0x28 + (x >> 3)
    assert planes[0][off] == (0x80 >> (x & 7))


def test_offscreen_culls():
    z = bytes(256)
    # X >= 0x140 culled
    planes = _planes()
    draw_particles(planes, [(0x140, 10, 0, 0)], 0, 0, 0, 0, z, z)
    assert sum(sum(pl) for pl in planes) == 0
    # Y >= 0xB0 culled
    planes = _planes()
    draw_particles(planes, [(10, 0xB0, 0, 0)], 0, 0, 0, 0, z, z)
    assert sum(sum(pl) for pl in planes) == 0


def test_camera_and_page_offset():
    z = bytes(256)
    planes = _planes()
    # cam (col=2,row=1) -> cam_x=32, cam_y=16; page 0x2000
    draw_particles(planes, [(64, 40, 0, 0)], cam_col=2, cam_row=1, y_bias=0,
                   page=0x2000, cos_table=z, sin_table=z)
    sx = 64 - 32
    sy = 40 - 16
    off = (0x2000 + sy * 0x28 + (sx >> 3)) & 0xFFFF
    assert planes[0][off] == (0x80 >> (sx & 7))
