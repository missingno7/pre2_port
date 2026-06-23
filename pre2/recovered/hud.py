"""Prehistorik 2 status-bar (HUD) rendering — recovered native primitives (pure).

The fixed status bar (score / lives / energy) is drawn into the bottom of the EGA page, below
the scrolling gameplay viewport (rows >= SCROLL_HEIGHT). Its dynamic glyphs — the score digits,
the lives count, the energy hearts — are blitted by ``1030:473D`` from a 16x12 planar glyph font.
This module recovers that glyph blit (the HUD's core leaf); the higher-level "which glyph where
from HudState" layout builds on it.

Pure: no ``cpu``/``mem``/``dos_re``. The font is a loaded asset (segment ``[0x3d]`` = 0x252B),
passed in as bytes; the VM<->memory translation lives in ``pre2/bridge``.
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = ["HUD_GLYPH_BASE", "HUD_GLYPH_BYTES", "HUD_GLYPH_ROWS", "blit_hud_glyph"]

HUD_GLYPH_BASE = 0xE60 + 0x7B0   # 0x1610 — font offset of glyph 0 [asm 4750/4753]
HUD_GLYPH_BYTES = 0x60           # 96 bytes/glyph = 4 planes x 12 rows x 2 bytes [asm 474C mul 0x60]
HUD_GLYPH_ROWS = 0x0C            # 12 rows [asm 475F cx=0xC]
_ROW_STRIDE = 0x28               # screen bytes per row


@oracle_link("1030:473D",
             "blit one 16x12 planar HUD glyph from the font (ds=[0x3d], glyph at "
             "0x1610 + glyph*0x60) to the four EGA planes at di (di preserved). Plane-major: "
             "for each of 4 planes, 12 rows x 2 bytes via the SC map mask.",
             "VERIFIED", merge_target="render_frame")
def blit_hud_glyph(planes, glyph, di, font):
    """Recover ``1030:473D`` — draw one HUD glyph (digit / heart / label char).

    ``planes`` = the four EGA plane buffers; ``glyph`` = the glyph index; ``di`` = screen byte
    offset; ``font`` = the HUD font segment bytes. Each glyph is stored plane-major (plane 0's
    12x2 bytes, then plane 1's, ...). ``di`` is left unchanged (the ASM resets it per plane).
    """
    src = HUD_GLYPH_BASE + (glyph & 0xFF) * HUD_GLYPH_BYTES
    for p in range(4):
        d = di & 0xFFFF
        for _row in range(HUD_GLYPH_ROWS):
            planes[p][d] = font[src]
            planes[p][(d + 1) & 0xFFFF] = font[src + 1]
            src += 2
            d = (d + _ROW_STRIDE) & 0xFFFF
