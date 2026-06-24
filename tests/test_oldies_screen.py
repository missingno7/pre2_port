"""Tests for the recovered OLDIES / credits screen (pre2.recovered.oldies_screen).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_oldies.py: the recovered render matches
the force-executed ASM credit drawers Δ=0). These lock the char->glyph mapping, the space-clear, and the
per-line cursor advance with hand-computed cases."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.oldies_screen import (  # noqa: E402
    blit_char, char_to_glyph, draw_credit_line, render_oldies)


def test_char_to_glyph_mapping():
    assert char_to_glyph(ord("0")) == 0          # digits direct
    assert char_to_glyph(ord("9")) == 9
    assert char_to_glyph(ord("<")) == 10         # 0x3C - 0x32
    assert char_to_glyph(ord(">")) == 12         # 0x3E - 0x32
    assert char_to_glyph(ord("A")) == 15         # 0x41 - 0x32
    assert char_to_glyph(ord("Z")) == 40


def test_blit_char_plane_major_8x12():
    # font glyph 15 ('A'): plane p, row 0 = 0x10+p; rest 0
    font = bytearray(64 * 0x30)
    for p in range(4):
        font[15 * 0x30 + p * 12] = 0x10 + p
    planes = [bytearray(0x10000) for _ in range(4)]
    blit_char(planes, ord("A"), di=0x500, font=bytes(font))
    for p in range(4):
        assert planes[p][0x500] == 0x10 + p          # row 0
        assert planes[p][0x500 + 0x28] == 0          # row 1 (only row 0 set)


def test_space_clears_cell():
    planes = [bytearray(0x10000) for _ in range(4)]
    for p in range(4):
        for r in range(12):
            planes[p][0x500 + r * 0x28] = 0xFF
    blit_char(planes, 0x20, di=0x500, font=bytes(64 * 0x30))
    for p in range(4):
        for r in range(12):
            assert planes[p][0x500 + r * 0x28] == 0


def test_draw_line_advances_cursor():
    # glyph 0 ('0') has plane0 row0 = 0xAA; draw "00" at row 0x140, indent 1 -> di 0x141, 0x142
    font = bytearray(64 * 0x30)
    font[0] = 0xAA
    planes = [bytearray(0x10000) for _ in range(4)]
    draw_credit_line(planes, row=0x140, indent=1, text="00", page=0, font=bytes(font))
    assert planes[0][0x141] == 0xAA
    assert planes[0][0x142] == 0xAA


def test_render_oldies_multiple_lines():
    font = bytearray(64 * 0x30)
    font[15 * 0x30] = 0x55     # 'A' plane0 row0
    planes = [bytearray(0x10000) for _ in range(4)]
    render_oldies(planes, [(0x100, 0, "A"), (0x200, 2, "A")], page=0, font=bytes(font))
    assert planes[0][0x100] == 0x55       # line 1, col 0
    assert planes[0][0x202] == 0x55       # line 2, col 2
