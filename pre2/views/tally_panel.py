"""Bridge: read the tally-panel inputs (score, completion counters) + resolve the glyph fonts.

The fonts are decoded asset data in VM memory (the HUD font segment [0x3d] for the digits; the glyph
DIRECTORY at 1A0F:0x5F48 (offsets) / 1A0F:0x62E8 (segments) for the big letter font) — bridge-fed like
draw_hud's font, not the VM framebuffer. The letter glyph directory is indexed by (char-'A')+0xF1
([asm 47CB-47D7]); the '%' symbol is the special glyph 0x1A (-> directory index 0x1A+0xF1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from pre2.recovered.tally_panel import compute_percent

_DATA = 0x1A0F
_DIGIT_FONT_OFF = 0x1C70       # [asm 4780] digit glyphs in the [0x3d] font segment
_DIGIT_SZ = 0x58
_DIR_OFFSETS = 0x5F48          # [asm 47D3] glyph-offset table (per directory index)
_DIR_SEGMENTS = 0x62E8         # [asm 47D7] glyph-segment table
_GLYPH_HDR = 0x16              # [asm 47DB] glyph data starts +0x16 past the directory offset
_GLYPH_LEN = 0x58              # 4 planes x 11 rows x 2 bytes
_LABEL_CHARS = "SCORELVMP TD"  # the distinct chars in "SCORE" + "LEVEL COMPLETED"
_PCT_GLYPH_IDX = 0x1A          # [asm 518A] '%' is letter-glyph 0x1A


@dataclass
class TallyPanelInputs:
    score: int
    percent: int
    digit_font: bytes
    letters: Dict[str, bytes]
    pct_glyph: bytes


def _r16(d, seg, off):
    a = ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF
    return d[a] | (d[(a + 1) & 0xFFFFF] << 8)


def _letter_glyph(d, dir_index: int) -> bytes:
    off = _r16(d, _DATA, _DIR_OFFSETS + dir_index * 2)
    seg = _r16(d, _DATA, _DIR_SEGMENTS + dir_index * 2)
    base = ((seg << 4) + ((off + _GLYPH_HDR) & 0xFFFF)) & 0xFFFFF
    return bytes(d[base:base + _GLYPH_LEN])


def read_tally_panel(mem) -> TallyPanelInputs:
    d = mem.data
    score = _r16(d, _DATA, 0x6C0E) | (_r16(d, _DATA, 0x6C10) << 16)
    percent = compute_percent(_r16(d, _DATA, 0x2A74), _r16(d, _DATA, 0x2A76),
                              _r16(d, _DATA, 0x2A78), _r16(d, _DATA, 0x2A7A))
    font_seg = _r16(d, _DATA, 0x3D)
    fbase = ((font_seg << 4) + _DIGIT_FONT_OFF) & 0xFFFFF
    digit_font = bytes(d[fbase:fbase + 10 * _DIGIT_SZ])
    letters = {ch: _letter_glyph(d, (ord(ch) - 0x41 + 0xF1) & 0xFFFF) for ch in _LABEL_CHARS if ch != " "}
    pct_glyph = _letter_glyph(d, (_PCT_GLYPH_IDX + 0xF1) & 0xFFFF)
    return TallyPanelInputs(score=score, percent=percent, digit_font=digit_font,
                            letters=letters, pct_glyph=pct_glyph)
