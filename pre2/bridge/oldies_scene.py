"""Bridge: the OLDIES / "programmed in 1992" easter-egg screen layout (1030:2417).

The real OLDIES screen (cyxx `do_programmed_in_1992_screen`, date-gated to system year >= 1996) draws:
  "YEAAA > > >" / "MY GAME IS STILL WORKING IN <year> <<" / "PROGRAMMED IN 1992 ON AT >286 12MHZ>" /
  "> > > ENJOY OLDIES<<"
via 1030:2417 -> 0xC31 (string) -> 0xC3E (glyph), with the year ([0x37], the live system year) drawn
inline by 0xBEF. (The separate names "credits" screen, 1030:2505 / cyxx `do_credits`, is `if (0)` —
disabled — so it is NOT part of this screen.)

The credit SCRIPT (row byte-offset, indent, string) is the constant layout from the ASM; the strings + the
8x12 font (segment [0x3d]) are bridge-fed data, not the VM framebuffer.
"""
from __future__ import annotations

from pre2.recovered.oldies_screen import format_year, render_oldies
from pre2.recovered.scene_compositor import RecoveredBackground, compose_scene

_DATA = 0x1A0F

# (row byte-offset [0x2385], start indent [0x2383], string offset in seg 1A0F)  [asm 2417]
_YEAAA = (0x0960, 5, 0x28CA)         # "YEAAA > > >"
_WORKING = (0x0B40, 0, 0x28D6)       # "MY GAME IS STILL WORKING IN" + <year> + " <<" (0x28F2)
_WORKING_TAIL = 0x28F2               # " <<"
_PROGRAMMED = (0x1680, 1, 0x28F6)    # "PROGRAMMED IN 1992 ON AT >286 12MHZ>"
_ENJOY = (0x1860, 3, 0x291B)         # "> > > ENJOY OLDIES<<"
_YEAR = 0x37                         # [0x37] = the live system year ([asm 2441: dx=[0x37]; call 0xBEF)


def _read_string(d, off: int) -> str:
    base = (_DATA << 4) + off
    out = []
    for k in range(48):
        c = d[(base + k) & 0xFFFFF]
        if c == 0:
            break
        out.append(chr(c))
    return "".join(out)


def read_oldies(mem):
    """Return (lines, font): the resolved OLDIES credit lines [(row, indent, text)] + the 8x12 font."""
    d = mem.data
    year = d[(_DATA << 4) + _YEAR] | (d[(_DATA << 4) + _YEAR + 1] << 8)
    # the "working in" line concatenates the message + the year + the tail on one row (cursor continues)
    working = _read_string(d, _WORKING[2]) + format_year(year) + _read_string(d, _WORKING_TAIL)
    lines = [
        (_YEAAA[0], _YEAAA[1], _read_string(d, _YEAAA[2])),
        (_WORKING[0], _WORKING[1], working),
        (_PROGRAMMED[0], _PROGRAMMED[1], _read_string(d, _PROGRAMMED[2])),
        (_ENJOY[0], _ENJOY[1], _read_string(d, _ENJOY[2])),
    ]
    font_seg = d[(_DATA << 4) + 0x3D] | (d[(_DATA << 4) + 0x3E] << 8)
    fbase = (font_seg << 4) & 0xFFFFF
    return lines, bytes(d[fbase:fbase + 0x1800])


def build_oldies_scene(mem, *, page):
    """Compose the OLDIES scene: black background + the recovered credit text."""
    lines, font = read_oldies(mem)
    bg = RecoveredBackground(tuple(bytes(0x10000) for _ in range(4)))
    return compose_scene(bg, [lambda pl, pg: render_oldies(pl, lines, pg, font)], page)
