"""The level-end TALLY text panel (1030:51A3 driver + its leaves) — "SCORE …" / "LEVEL COMPLETED …%".

The tally screen draws a two-line text panel over the black background (the moving player/items are the
object pass). It is a second HUD-like panel, NOT draw_string: a digit-glyph blit (4780), a letter-glyph
blit (47C0, big font via the glyph directory), a 32-bit number->digit-string converter (4803), a
percentage computation (5139), and the driver (51A3) that lays it all out.

Recovered leaves (all plane-major glyphs: 4 planes x 11 rows x 2 bytes = 88 = 0x58):
  * ``blit_glyph``        — one 16x11 glyph at ``di`` across the 4 planes (di stride 0x28/row, reset per
    plane); the shared body of 4780 (digit font [0x3d]:0x1C70, glyph*0x58) and 47C0 (letter font via the
    directory, glyph data at seg:off+0x16).
  * ``number_to_digits``  — 4803: a 32-bit value -> 10 ASCII digits (leading zeros) + a '$' terminator.
  * ``compute_percent``   — 5139: (collected & 0xff) * 100 / total, collected=[0x2A76]+[0x2A7A],
    total=[0x2A74]+[0x2A78].
  * ``render_tally_panel`` — 51A3/5139: "SCORE" + the 6-digit score (internal, +trailing '0' = *10),
    then "LEVEL COMPLETED" + the percentage + '%'.

The fonts (digit glyphs + the letter glyph directory) are bridge-fed asset bytes, exactly like draw_hud's
font — NOT the VM framebuffer.
"""
from __future__ import annotations

from typing import Dict, Sequence

from pre2.islands import oracle_link

_GLYPH_ROWS = 11
_ROW = 0x28
_DIGIT_SZ = 0x58

# [asm 51A6/51BE/5139/516C] panel layout (offsets from the page base)
_SCORE_LABEL_DI = 0x230
_SCORE_DIGITS_DI = 0x23C
_PCT_LABEL_DI = 0x410
_PCT_DIGITS_DI = 0x430
_SCORE_LABEL = "SCORE"           # [asm 1A0F:0x7D8F]
_PCT_LABEL = "LEVEL COMPLETED"   # [asm 1A0F:0x7D7F]


def blit_glyph(planes: Sequence[bytearray], glyph_bytes: bytes, di: int) -> None:
    """Blit one 16x11 plane-major glyph (88 bytes) at ``di`` — the shared 4780/47C0 inner blit."""
    src = 0
    for p in range(4):
        d = di & 0xFFFF
        for _row in range(_GLYPH_ROWS):
            planes[p][d] = glyph_bytes[src]
            planes[p][(d + 1) & 0xFFFF] = glyph_bytes[src + 1]
            src += 2
            d = (d + _ROW) & 0xFFFF


def number_to_digits(value: int) -> bytes:
    """Recover 4803: a 32-bit ``value`` -> 10 ASCII digits (leading zeros) + a '$' (0x24) terminator."""
    value &= 0xFFFFFFFF
    out = bytearray()
    for power in (1000000000, 100000000, 10000000, 1000000, 100000, 10000, 1000, 100, 10, 1):
        d = 0
        while value >= power:
            value -= power
            d += 1
        out.append(0x30 + d)
    out.append(0x24)
    return bytes(out)


def compute_percent(c74: int, c76: int, c78: int, c7a: int) -> int:
    """Recover 5139: level-completed % = (collected & 0xff) * 100 / total (0 if total==0)."""
    total = (c74 + c78) & 0xFFFF
    if total == 0:
        return 0
    collected = (c76 + c7a) & 0xFFFF
    return ((collected & 0xFF) * 100) // total


@oracle_link("1030:51A3",
             "level-end TALLY text panel: draw 'SCORE' (letter glyphs, 47C0) + the score (4803 -> the "
             "low 6 digits of [0x6C0E]/[0x6C10] + a trailing '0' = internal*10, digit glyphs 4780), then "
             "'LEVEL COMPLETED' + the percentage (5139: (collected&0xff)*100/total from [0x2A74..0x2A7A], "
             "leading zero skipped) + the '%' glyph. Plane-major 16x11 glyphs; fonts bridge-fed.",
             "VERIFIED", merge_target="render_scene")
def render_tally_panel(planes: Sequence[bytearray], score: int, percent: int, page: int,
                       digit_font: bytes, letters: Dict[str, bytes], pct_glyph: bytes) -> None:
    """Draw the tally panel. ``digit_font`` = the 10 digit glyphs (0x58 each); ``letters`` = {char: 88-byte
    glyph} for the label chars; ``pct_glyph`` = the '%' glyph."""
    def _digit(d, di):
        blit_glyph(planes, digit_font[d * _DIGIT_SZ:(d + 1) * _DIGIT_SZ], di)

    def _label(text, di):
        for ch in text:                              # [asm 5190] space advances, no glyph
            if ch != " ":
                blit_glyph(planes, letters[ch], di)
            di = (di + 2) & 0xFFFF

    # line 1: "SCORE" + the score digits (low 6 of the 10-digit string [asm si=0x6F52] + trailing '0')
    _label(_SCORE_LABEL, (_SCORE_LABEL_DI + page) & 0xFFFF)
    di = (_SCORE_DIGITS_DI + page) & 0xFFFF
    for d in number_to_digits(score)[4:]:            # [asm si=0x6F52 = digits+4]
        if d == 0x24:
            break
        _digit(d - 0x30, di)
        di = (di + 2) & 0xFFFF
    _digit(0, di)                                    # [asm 51D6] trailing '0'

    # line 2: "LEVEL COMPLETED" + the percentage (last 3 of the digit string, leading zero skipped) + '%'
    _label(_PCT_LABEL, (_PCT_LABEL_DI + page) & 0xFFFF)
    di = (_PCT_DIGITS_DI + page) & 0xFFFF
    pbuf = number_to_digits(percent)[7:]             # [asm si=0x6F55 = digits+7]
    if pbuf and pbuf[0] == 0x30:                     # [asm 5176] skip a single leading zero
        pbuf = pbuf[1:]
    for d in pbuf:
        if d == 0x24:
            break
        _digit(d - 0x30, di)
        di = (di + 2) & 0xFFFF
    blit_glyph(planes, pct_glyph, di)                # [asm 518A] the '%' glyph
