"""Control-flow tests for the recovered text/font renderer (pre2.recovered.text).

These guard the parts read unambiguously from the disassembly: the ASCII->glyph mapping,
the terminator, the per-char pen advance, and the per-glyph blit dispatch. Byte-exact
pixel fidelity is RECOVERED-but-not-VERIFIED (the font glyphs / VGA state are not present
in any post-draw snapshot — see the module docstring's NEEDS-REPRO note), so it is not
checked here.
"""
from __future__ import annotations

import pre2.recovered.text as text
from pre2.recovered.text import (
    GLYPH_BYTES, GLYPH_HEADER, SPACE_GLYPH, draw_string, glyph_index,
)


def test_glyph_index_mapping():
    assert glyph_index(0x20) == SPACE_GLYPH          # space -> 0x2B
    assert glyph_index(ord("0")) == 0
    assert glyph_index(ord("9")) == 9
    assert glyph_index(ord("A")) == 0x0A             # ch - 0x37
    assert glyph_index(ord("Z")) == 0x0A + 25
    assert glyph_index(0x00) is None                 # NUL terminates
    assert glyph_index(ord("!")) is None             # any byte < '0' (not space) terminates


def test_draw_string_control_flow():
    calls = []
    real = text._blit_glyph
    text._blit_glyph = lambda *a, **k: calls.append((a[2], a[3], a[4]))  # (src, di_draw, di_clear)
    try:
        pen = draw_string([None] * 4, b"0A 9\x00END", font=b"", font_base=0x4200,
                          pen=0x100, advance=4, page_draw=0x19, page_clear=0x19)
    finally:
        text._blit_glyph = real

    # "0A 9" -> 4 glyphs, then 0x00 terminates (the "END" tail is never reached).
    assert len(calls) == 4
    want_idx = (0, 0x0A, SPACE_GLYPH, 9)
    assert [c[0] for c in calls] == [0x4200 + gi * GLYPH_BYTES + GLYPH_HEADER for gi in want_idx]
    # the pen advances by `advance` BEFORE each glyph; di = (pen + 0x50 + page) & 0x1FFF.
    for k, c in enumerate(calls):
        assert c[1] == (0x100 + 4 * (k + 1) + 0x50 + 0x19) & 0x1FFF   # di_draw  (page_draw)
        assert c[2] == (0x100 + 4 * (k + 1) + 0x50 + 0x19) & 0x1FFF   # di_clear (page_clear)
    assert pen == (0x100 + 4 * 4) & 0xFFFF


def test_draw_string_stops_at_terminator():
    calls = []
    real = text._blit_glyph
    text._blit_glyph = lambda *a, **k: calls.append(a[2])
    try:
        draw_string([None] * 4, b"!immediately", font=b"", font_base=0, pen=0,
                    advance=4, page_draw=0, page_clear=0)
    finally:
        text._blit_glyph = real
    assert calls == []  # '!' (< '0') terminates before any glyph
