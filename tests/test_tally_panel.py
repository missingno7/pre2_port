"""Tests for the recovered tally text panel (pre2.recovered.tally_panel).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_tally_panel.py: the panel rows match the
ASM Δ=0 at a settled tally frame). These lock the number->digits converter, the % math, and the glyph
plot, with hand-computed cases."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.tally_panel import (  # noqa: E402
    blit_glyph, compute_percent, number_to_digits, render_tally_panel)


def test_number_to_digits_leading_zeros_and_terminator():
    assert number_to_digits(3395) == b"0000003395\x24"
    assert number_to_digits(0) == b"0000000000\x24"
    assert number_to_digits(27) == b"0000000027\x24"
    assert number_to_digits(1234567890) == b"1234567890\x24"


def test_compute_percent():
    # collected=(c76+c7a), total=(c74+c78); % = (collected&0xff)*100/total
    assert compute_percent(100, 27, 0, 0) == 27          # 27*100/100
    assert compute_percent(50, 5, 50, 5) == 10           # collected 10, total 100 -> 10%
    assert compute_percent(0, 5, 0, 0) == 0              # total 0 -> 0 (no div by zero)
    assert compute_percent(3, 1, 0, 0) == 33             # 1*100//3 = 33


def test_blit_glyph_plane_major_88_bytes():
    planes = [bytearray(0x10000) for _ in range(4)]
    # a glyph whose plane p, row 0 = (0x10+p, 0x20+p); rest zero
    gb = bytearray(0x58)
    for p in range(4):
        gb[p * 22] = 0x10 + p
        gb[p * 22 + 1] = 0x20 + p
    blit_glyph(planes, bytes(gb), di=0x1000)
    for p in range(4):
        assert planes[p][0x1000] == 0x10 + p
        assert planes[p][0x1001] == 0x20 + p
        assert planes[p][0x1000 + 0x28] == 0  # row 1 (this glyph only set row 0)


def test_render_panel_draws_score_and_percent():
    # minimal fonts: digit glyph d marks plane0 row0 byte0 = 0xD0+d; letters/% mark 0xLL
    digit_font = bytearray(10 * 0x58)
    for d in range(10):
        digit_font[d * 0x58] = 0xD0 + d
    letters = {ch: bytes([0xAA]) + bytes(0x57) for ch in "SCORELVMP TD"}
    pct = bytes([0xBB]) + bytes(0x57)
    planes = [bytearray(0x10000) for _ in range(4)]
    render_tally_panel(planes, score=3395, percent=27, page=0,
                       digit_font=bytes(digit_font), letters=letters, pct_glyph=pct)
    # score line digits start at 0x23C: "003395" + trailing 0 -> first digit '0' glyph (0xD0)
    assert planes[0][0x23C] == 0xD0
    # the score's 3rd drawn digit is '3' (0033 95 0 -> index2 = '3')
    assert planes[0][0x23C + 4] == 0xD3
    # % digits at 0x430: "27" (leading zero skipped) -> '2' then '7'
    assert planes[0][0x430] == 0xD2
    assert planes[0][0x432] == 0xD7
