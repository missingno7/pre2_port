"""Prehistorik 2 text / font renderer — recovered logic (pure).

Recovers ``draw_string`` (``1030:9886``), the bitmap-font string drawer used by the
non-gameplay scenes (title / "oldies" / score / tally text). It walks a string, maps each
character to a glyph, and blits a fixed 24x12-pixel cell per character into **VGA planes 2
and 3 only** (a 2-bits-per-pixel font), advancing the pen by a per-call width.

Confidence: **VERIFIED.** Confirmed two ways: (1) every instruction ``1030:9886``..``98FF``
traced statically (see the per-line ``[asm]`` notes — clear loop 12×3 into planes 2|3 via seq
map-mask ``0x0C`` stride ``0x28`` ``di &= 0x1FFF``; draw loop plane 2 ``src+0`` / plane 3
``src+0x30`` 36 bytes each, ``di`` reset per plane, the ``add ax,6`` header skip); (2) **runtime
lockstep — 24/24 menu text draws byte-exact, 0 divergence** ("MODE", "BEGINNER", …) by replaying
``demo_pre2_20260622_192206`` (which navigates the menu) and diffing planes 2|3 vs the ASM
(``pre2/probes/capture_text_draw.py``). The witness had to be reached by demo replay because
``draw_string`` fires only on menu/score/tally **redraws**, never on cold boot or steady
gameplay. Menu observations: each item is drawn to **both display pages** (page ``0x0`` and
``0x1FFF``, double-buffered); the **cursor highlight is a shade swap** — the selected item is
re-drawn with a different ``font_base`` (``0x4200`` vs ``0x0``), not a separate marker. Not yet
wired live (no menu in the live gameplay path), but trusted for the scene seam.

Char mapping (`[asm 988F-989F]`): ``' '`` (0x20) -> glyph 0x2B; ``'0'..'9'`` -> 0..9;
``'A'..'Z'`` -> 0x0A.. (``ch - 0x37``). **Any other byte ends the string** (terminator):
``ch < 0x30`` (other than space) falls through the `jb` at 0x9899 to the RET.

Pure: no ``cpu``/``mem``/``dos_re`` imports. The font glyph bytes + plane buffers + layout
constants are passed in; the VM<->memory translation belongs in ``pre2/bridge/``.
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = [
    "GLYPH_BYTES", "GLYPH_HEADER", "PLANE_BLOCK", "CELL_BYTES", "CELL_ROWS",
    "ROW_STRIDE", "WRAP_MASK", "SPACE_GLYPH", "glyph_index", "draw_string",
]

GLYPH_BYTES = 0x60      # bytes per glyph in the font [asm 98A1: ah=0x60; mul]
GLYPH_HEADER = 6        # skipped before the pixel rows [asm 98A9: add ax,6]
PLANE_BLOCK = 0x30      # source stride between the two plane blocks (36 used + 12 skip)
CELL_BYTES = 3          # bytes written per row = 24 px [asm: movsw + movsb]
CELL_ROWS = 0x0C        # 12 rows per glyph cell [asm 98CD/98DF: cx=0xC]
ROW_STRIDE = 0x28       # screen bytes per row (3 written + 0x25 added)
WRAP_MASK = 0x1FFF      # in-page offset wrap [asm 98D2: and di,bp ; bp=0x1FFF (9AE0)]
SPACE_GLYPH = 0x2B      # space maps here [asm 9893: al=0x2B]
# The font uses VGA sequencer map-mask planes 2 and 3 (the clear is map-mask 0x0C =
# planes 2|3; the two draw passes are map-mask 4 then 8 = plane 2 then plane 3).
TEXT_PLANES = (2, 3)


def glyph_index(ch: int) -> int | None:
    """Map an ASCII byte to a font glyph index, or ``None`` if it ends the string.

    [asm 988F-989F]: space -> SPACE_GLYPH; '0'..'9' -> 0..9; 'A'.. -> ch-0x37; any byte
    below '0' (other than space) terminates.
    """
    if ch == 0x20:                          # [asm 988F: cmp al,0x20 / 9893]
        return SPACE_GLYPH
    v = ch - 0x30                           # [asm 9897: sub al,0x30]
    if v < 0:                               # [asm 9899: jb -> RET] terminator
        return None
    if v <= 9:                              # [asm 989B: cmp al,9 / jbe] digit
        return v
    return v - 7                            # [asm 989F: sub al,7] letter ('A'->0x0A)


def _blit_glyph(planes, font, src, di_draw, di_clear,
                wrap=WRAP_MASK, stride=ROW_STRIDE):
    """Blit one glyph cell: clear planes 2|3 then copy the two plane blocks. [asm 98AC-98F6]

    ``src`` indexes ``font`` at the glyph's pixel data (``base + idx*0x60 + 6``). The clear
    uses ``di_clear`` (page ``[0xB1A3]``); the draw uses ``di_draw`` (page ``[0xB1A1]``);
    each row offset is masked to the page (``& wrap``) before writing.
    """
    d = di_clear                                            # [asm 98BF]
    for _r in range(CELL_ROWS):                             # clear loop [asm 98D2-98D9]
        d &= wrap
        for b in range(CELL_BYTES):
            planes[2][d + b] = 0
            planes[3][d + b] = 0
        d = (d + stride) & 0xFFFF
    for plane, block in zip(TEXT_PLANES, (0, PLANE_BLOCK)):  # draw loop [asm 98DE-98F6]
        d = di_draw
        s = src + block
        for _r in range(CELL_ROWS):
            d &= wrap
            for b in range(CELL_BYTES):
                planes[plane][d + b] = font[s + b]
            s += CELL_BYTES
            d = (d + stride) & 0xFFFF


@oracle_link("1030:9886",
             "draw a bitmap-font string into VGA planes 2|3: per char map ASCII->glyph "
             "(space=0x2B, 0-9, A-Z), clear a 24x12 cell then copy the glyph's two plane "
             "blocks, advance the pen by `advance` bytes; any byte < '0' (not space) ends "
             "the string. Font glyph = font_base + idx*0x60 + 6.",
             "RECOVERED", merge_target="text renderer")
def draw_string(planes, text, font, font_base, pen, advance,
                page_draw, page_clear, wrap=WRAP_MASK, stride=ROW_STRIDE):
    """Recover ``1030:9886`` — draw a font string. Writes planes 2 and 3 only.

    ``text`` is the raw bytes (drawing stops at the first terminator byte); ``font`` is the
    glyph segment's bytes; ``font_base`` is the per-shade base (``[0xB1AC]``); ``pen`` is the
    starting byte X (``[0xB1A6]``); ``advance`` is the per-char width in bytes (``[0xB1AB]``);
    ``page_draw``/``page_clear`` are the destination page offsets (``[0xB1A1]``/``[0xB1A3]``).
    Returns the final pen position. See the module docstring for the RECOVERED/verify status.
    """
    for ch in text:
        gi = glyph_index(ch)
        if gi is None:                                      # [asm 9899: terminator -> RET]
            break
        src = font_base + gi * GLYPH_BYTES + GLYPH_HEADER   # [asm 98A1-98AC]
        pen = (pen + advance) & 0xFFFF                      # [asm 98AE: [B1A6] += dx]
        base = (pen + 0x50) & 0xFFFF                        # [asm 98B5: + 0x50]
        di_draw = (base + page_draw) & 0xFFFF               # [asm 98BA push: + [B1A1]]
        di_clear = (base + page_clear) & 0xFFFF             # [asm 98BF: + [B1A3]]
        _blit_glyph(planes, font, src, di_draw, di_clear, wrap, stride)
    return pen
