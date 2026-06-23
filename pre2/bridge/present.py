"""VM↔memory layout for the scene-present leaves (mode-select / map scroll, 1030:9600).

Layout only — reads the scroll counter + the master-pattern segment, advances the scroll
counter, and writes the EGA planes. The blit/scroll *math* lives in
``pre2.recovered.present`` (``scroll_blit_column``).
"""
from __future__ import annotations

from pre2.bridge.object_render import read_planes  # noqa: F401 — re-export
from pre2.bridge.sprites import plane_views  # noqa: F401 — re-export (writable VRAM views)

_DS = 0x1A0F                # DGROUP segment (GOG build)
_SCROLL_X = 0xB19D         # horizontal scroll counter (pre-increment value drives this frame)
_FONT_SEG = 0x2875         # master-pattern / font segment (the scroll-blit source)


def _rw(mem, off: int) -> int:
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def _ww(mem, off: int, val: int) -> None:
    b = (_DS << 4) + off
    mem.data[b] = val & 0xFF
    mem.data[b + 1] = (val >> 8) & 0xFF


def read_scroll_inputs(mem):
    """Read the scroll-blit inputs at block entry (1030:965A): the pre-increment scroll_x
    (``[0xB19D]``) and the master-pattern segment bytes (``[0x2875]``)."""
    sx = _rw(mem, _SCROLL_X)
    fbase = (_rw(mem, _FONT_SEG) << 4) & 0xFFFFF
    source = bytes(mem.data[fbase:fbase + 0x10000])
    return sx, source


def advance_scroll_x(mem, sx: int) -> None:
    """Replicate ``[asm 965E: inc [0xB19D]]`` — advance the scroll counter past the value
    this frame's blit used."""
    _ww(mem, _SCROLL_X, (sx + 1) & 0xFFFF)


def _rb(mem, off: int) -> int:
    return mem.data[(_DS << 4) + off]


def read_scroll_shift_inputs(mem):
    """Inputs for the menu/scene framebuffer scroll (1030:9804): the horizontal-boundary
    byte ``[0xB199]``, ``scroll_x`` ``[0xB19D]``, ``scroll_y`` ``[0xB19F]``, the previous
    ``scroll_y`` ``[0xB19B]``, and ``page_draw`` ``[0xB1A1]``. The wrap mask is the live
    ``bp`` register, captured by the checkpoint (not a memory var)."""
    return (_rb(mem, 0xB199), _rw(mem, 0xB19D), _rw(mem, 0xB19F),
            _rw(mem, 0xB19B), _rw(mem, 0xB1A1))
