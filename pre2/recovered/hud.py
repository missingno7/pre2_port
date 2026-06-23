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

__all__ = ["HUD_GLYPH_BASE", "HUD_GLYPH_BYTES", "HUD_GLYPH_ROWS", "blit_hud_glyph",
           "HUD_LIVES_DI", "HUD_SCORE_DI", "HUD_ENERGY_DI", "HUD_MAX_HEARTS", "draw_hud",
           "HUD_BAR_DI", "HUD_BAR_PLANE_BYTES", "draw_status_bar"]

# Static status-bar background blit (1030:4580). The bar is a 320x23 planar bitmap.
HUD_BAR_DI = 0x1B80              # status-bar top-left screen offset within the page (row 176)
HUD_BAR_PLANE_BYTES = 0x398     # 0x398 = 40 bytes/row x 23 rows, per plane [asm 4587 cx=0x1CC words]

# Dynamic status-bar layout (1030:45B8). Screen byte offsets within the page (add the page base):
HUD_LIVES_DI = 0x1CED            # lives: one digit [asm 45FB]
HUD_SCORE_DI = 0x1CF1            # score: 6 digits + a fixed trailing 0 [asm 462F]
HUD_ENERGY_DI = 0x1D01           # energy: up to MAX_HEARTS hearts [asm 465C]
HUD_MAX_HEARTS = 3               # [asm 465A dl=3]
_HEART_FULL = 0x0A               # full-heart glyph [asm 4667]
_HEART_EMPTY = 0x0B              # empty-heart glyph [asm 4678]
_SCORE_DIGITS = 6                # digits drawn from [0x6F52] before the trailing 0
# BONUS letters B/O/N/U/S — glyphs 0x0C..0x10 at fixed page-relative di [asm 46AD loop, table 0x6F86],
# each drawn only if its bit is set in the effective mask (1030:46AD `shr ah,1; jae skip`).
HUD_BONUS_DI = (0x1C91, 0x1BF2, 0x1CE3, 0x1C6C, 0x1C1D)   # [0x6F86] B,O,N,U,S positions
_BONUS_GLYPH0 = 0x0C             # first BONUS-letter glyph (B) [asm 46AD al=0xC]

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


@oracle_link("1030:45B8",
             "dynamic status-bar layout: draw the lives digit (0x1CED), the 6-digit score + a "
             "fixed trailing 0 (0x1CF1; displayed = internal*10), and the energy hearts (0x1D01, "
             "`energy` full glyph 0x0A + the rest empty glyph 0x0B) via the 473D glyph blit.",
             "VERIFIED", merge_target="render_frame")
def draw_hud(planes, hud, font, page=0):
    """Recover the dynamic part of ``1030:45B8`` — draw the status-bar values from ``HudState``.

    ``hud`` carries ``score`` (displayed = internal*10), ``lives``, ``energy``; ``font`` is the
    HUD glyph font bytes; ``page`` is the EGA page base offset. Draws onto the (separately drawn)
    static status bar. Does not interpolate or read prior render output.
    """
    # lives — one digit, clamped to 9 (the one-digit field)  [asm 45F5]
    blit_hud_glyph(planes, min(hud.lives, 9) & 0xFF, (HUD_LIVES_DI + page) & 0xFFFF, font)
    # score — the internal value (=displayed//10) as 6 zero-padded digits, then a fixed trailing 0
    di = (HUD_SCORE_DI + page) & 0xFFFF
    for ch in f"{(hud.score // 10) % (10 ** _SCORE_DIGITS):0{_SCORE_DIGITS}d}":
        blit_hud_glyph(planes, ord(ch) - 0x30, di, font)
        di = (di + 2) & 0xFFFF
    blit_hud_glyph(planes, 0, di, font)
    # energy — `energy` full hearts then the remainder empty, up to MAX_HEARTS
    di = (HUD_ENERGY_DI + page) & 0xFFFF
    full = min(max(hud.energy, 0), HUD_MAX_HEARTS)
    for _ in range(full):
        blit_hud_glyph(planes, _HEART_FULL, di, font)
        di = (di + 2) & 0xFFFF
    for _ in range(HUD_MAX_HEARTS - full):
        blit_hud_glyph(planes, _HEART_EMPTY, di, font)
        di = (di + 2) & 0xFFFF
    # BONUS letters — glyph 0xC+i at its fixed di, drawn only if collected (bit i of the mask) [asm 46AD]
    for i, bdi in enumerate(HUD_BONUS_DI):
        if (hud.bonus_mask >> i) & 1:
            blit_hud_glyph(planes, (_BONUS_GLYPH0 + i) & 0xFF, (bdi + page) & 0xFFFF, font)


@oracle_link("1030:4580",
             "blit the static HUD chrome (320x23 planar status-bar bitmap) into page+0x1B80: per "
             "plane (SC map mask), rep movsw 0x398 bytes contiguously from the bar asset. "
             "Page-targeted; the original also duplicates to the other page (a page-system detail).",
             "VERIFIED", merge_target="render_frame")
def draw_status_bar(planes, page, bar):
    """Recover ``1030:4580`` (page-targeted) — draw the static status-bar background into ``page``.

    ``bar`` is the 320x23 planar bitmap (4 planes x ``HUD_BAR_PLANE_BYTES``, plane-major); ``page``
    is the EGA page base offset. Pure asset blit — no reuse of previously rendered pixels. The
    original ASM duplicates the bar to both display pages; that double-copy is left to a VM-faithful
    wrapper (the recovered renderer describes the bar in one target frame)."""
    di = (page + HUD_BAR_DI) & 0xFFFF
    n = HUD_BAR_PLANE_BYTES
    for p in range(4):
        s = p * n
        planes[p][di:di + n] = bar[s:s + n]
