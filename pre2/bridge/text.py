"""VM↔memory layout for the bitmap-font string drawer (1030:9886).

Layout only — it reads the string (``DS:BX``) + the font segment + the text state block
from DGROUP, and writes the pen back. The glyph/plane *math* lives in
``pre2.recovered.text`` (``draw_string``).

Contract at the routine's RET (98FF): planes 2|3 are written, the pen ``[0xB1A6]`` is
advanced one ``[0xB1AB]`` step per drawn char, ``bx`` points past the terminator byte, and
``ds`` is restored to DGROUP (the routine reloads ``0x1A0F`` at 98F8). ``ax/cx/dx/si/di`` are
clobbered scratch.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.bridge.object_render import read_planes  # noqa: F401 — re-export
from pre2.bridge.sprites import plane_views  # noqa: F401 — re-export (writable VRAM views)
from pre2.recovered.text import glyph_index

_DS = 0x1A0F                # DGROUP segment (GOG build) — the text state block + string
_FONT_SEG = 0x2875         # [asm 98C3] font glyph segment (word)
_FONT_BASE = 0xB1AC        # [asm 98A5] per-shade glyph base (word)
_PEN = 0xB1A6              # [asm 98AE/98B2] pen byte-X (word)
_ADVANCE = 0xB1AB          # [asm 9889] per-char width (byte)
_PAGE_DRAW = 0xB1A1        # [asm 98BA] draw page offset (word)
_PAGE_CLEAR = 0xB1A3       # [asm 98BF] clear page offset (word)
_STR_MAX = 64              # max string bytes to read (draw_string stops at the terminator)


@dataclass(frozen=True)
class TextInputs:
    """One draw_string call's inputs: the raw string bytes, the font segment bytes, and
    the per-shade base / pen / advance / page offsets from DGROUP."""
    text: bytes
    font: bytes
    font_base: int
    pen: int
    advance: int
    page_draw: int
    page_clear: int


def _rb(mem, off: int) -> int:
    return mem.data[(_DS << 4) + off]


def _rw(mem, off: int) -> int:
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def _ww(mem, off: int, val: int) -> None:
    b = (_DS << 4) + off
    mem.data[b] = val & 0xFF
    mem.data[b + 1] = (val >> 8) & 0xFF


def read_text_inputs(mem, ds: int, bx: int) -> TextInputs:
    """Read one draw_string call's inputs. ``ds:bx`` is the caller's string pointer (the
    string lives in DGROUP); the state block + font segment are read from DGROUP."""
    sbase = ((ds << 4) + bx) & 0xFFFFF
    text = bytes(mem.data[sbase:sbase + _STR_MAX])
    font_seg = _rw(mem, _FONT_SEG)
    fbase = (font_seg << 4) & 0xFFFFF
    font = bytes(mem.data[fbase:fbase + 0x10000])
    return TextInputs(text, font, _rw(mem, _FONT_BASE), _rw(mem, _PEN),
                      _rb(mem, _ADVANCE), _rw(mem, _PAGE_DRAW), _rw(mem, _PAGE_CLEAR))


def write_pen(mem, pen: int) -> None:
    """Write the advanced pen back to ``[0xB1A6]``."""
    _ww(mem, _PEN, pen)


def consumed_bytes(text: bytes) -> int:
    """How many bytes the ASM reads from ``DS:BX`` — every char up to and including the
    first terminator (it does ``inc bx`` before the terminator test). Used to advance the
    live ``bx`` exactly like the routine."""
    for i, ch in enumerate(text):
        if glyph_index(ch) is None:
            return i + 1
    return len(text)
