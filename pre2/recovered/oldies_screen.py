"""The OLDIES / credits screen (1030:2505 / 244E + the 0xC31/0xC3E text leaves) — black bg + glyph text.

The "oldies" attract screen is a black 0Dh page with several centred lines of bitmap text (the credits:
"CODER> DESIGNER …", "ERIC ZMIRO", … "ENJOY OLDIES<<"). Each line is drawn char-by-char by 0xC31 ->
0xC3E using an 8x12 4-plane font in segment [0x3d].

0xC3E (one char):
  * di = line_row (`[0x2385]`) + x_cursor (`[0x2383]`) + page (`[0x2DD6]`); the x cursor advances 1 byte
    per char ([asm 0C47-0C53]).
  * space (0x20) clears a 12-row x 1-byte cell ([asm 0C5B]).
  * otherwise glyph = char-0x30 if that is <=9 (digits) else char-0x32 ([asm 0C7B], skipping ':' ';'),
    and the 8x12 4-plane glyph (plane-major, 12 bytes/plane) at font[glyph*0x30] is blitted ([asm 0C9F]).

The layout (row, indent, string) is the credits SCRIPT ([asm 2505/244E], the rows are byte offsets, the
indents are start columns); the strings + font are bridge-fed data.
"""
from __future__ import annotations

from typing import Sequence, Tuple

from pre2.islands import oracle_link

_GLYPH_SZ = 0x30          # 4 planes x 12 rows x 1 byte
_ROWS = 12
_ROW = 0x28

# (row byte-offset, start indent column, text); the credits script. [asm 2505 then 244E]
CreditLine = Tuple[int, int, str]


def char_to_glyph(ch: int) -> int:
    """Map an ASCII byte to a font glyph index ([asm 0C7B]): digits 0-9 direct, else char-0x32."""
    al = (ch - 0x30) & 0xFF
    return al if al <= 9 else (al - 2) & 0xFF


def blit_char(planes: Sequence[bytearray], ch: int, di: int, font: bytes) -> None:
    """Blit (or clear, for space) one 8x12 4-plane char cell at ``di`` ([asm 0C57])."""
    if ch == 0x20:                                   # space: clear the cell on all planes
        for p in range(4):
            d = di & 0xFFFF
            for _r in range(_ROWS):
                planes[p][d] = 0
                d = (d + _ROW) & 0xFFFF
        return
    src = char_to_glyph(ch) * _GLYPH_SZ
    for p in range(4):
        d = di & 0xFFFF
        for _r in range(_ROWS):
            planes[p][d] = font[src]
            src += 1
            d = (d + _ROW) & 0xFFFF


def draw_credit_line(planes: Sequence[bytearray], row: int, indent: int, text: str, page: int,
                     font: bytes) -> None:
    """Draw one credit line: char k at di = row + (indent + k) + page ([asm 0C31 loop -> 0C3E])."""
    col = indent
    for ch in text:
        blit_char(planes, ord(ch), (row + col + page) & 0xFFFF, font)
        col += 1


@oracle_link("1030:2505",
             "OLDIES / credits screen: draw the centred credit lines (the 2505 names script + the 244E "
             "header) as 8x12 4-plane glyph text on the black 0Dh page. Each char via 0xC3E (glyph = "
             "char-0x30 for digits else char-0x32, font [0x3d]:glyph*0x30, plane-major); di = line row "
             "[0x2385] + x cursor [0x2383] + page [0x2DD6], x advancing per char.",
             "VERIFIED", merge_target="render_scene")
def render_oldies(planes: Sequence[bytearray], lines: Sequence[CreditLine], page: int, font: bytes) -> None:
    """Draw all credit ``lines`` onto ``planes`` (the black page) ."""
    for row, indent, text in lines:
        draw_credit_line(planes, row, indent, text, page, font)
