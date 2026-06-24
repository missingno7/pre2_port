"""Bridge: the OLDIES / credits screen layout + reading the strings and font from VM data.

The credit SCRIPT (row byte-offset, start indent, string offset) is the constant layout from the ASM
([asm 2505 names + 244E header]); the strings live in the data segment and the 8x12 font in segment
[0x3d] — both bridge-fed data (like draw_hud's font), not the VM framebuffer.
"""
from __future__ import annotations

from pre2.recovered.oldies_screen import render_oldies
from pre2.recovered.scene_compositor import RecoveredBackground, compose_scene

_DATA = 0x1A0F

# (row byte-offset [0x2385], start indent [0x2383], string offset in seg 1A0F)  [asm 2505 then 244E]
_LAYOUT = (
    (0x0140, 1, 0x2930), (0x0370, 14, 0x2955), (0x07D0, 4, 0x2960), (0x0A00, 11, 0x297F),
    (0x0E60, 9, 0x2990), (0x1090, 11, 0x29A5), (0x1770, 15, 0x29B6), (0x1A40, 2, 0x29C0),
    (0x1C20, 0, 0x29E4),                                                # 2505 (names)
    (0x1680, 1, 0x28F6), (0x1860, 3, 0x291B),                          # 244E (header)
)


def _read_string(d, off: int) -> str:
    base = (_DATA << 4) + off
    out = []
    for k in range(64):
        c = d[(base + k) & 0xFFFFF]
        if c == 0:
            break
        out.append(chr(c))
    return "".join(out)


def read_oldies(mem):
    """Return (lines, font): the resolved credit lines [(row, indent, text)] and the 8x12 font bytes."""
    d = mem.data
    lines = [(row, indent, _read_string(d, soff)) for row, indent, soff in _LAYOUT]
    font_seg = d[(_DATA << 4) + 0x3D] | (d[(_DATA << 4) + 0x3E] << 8)
    fbase = (font_seg << 4) & 0xFFFFF
    font = bytes(d[fbase:fbase + 0x1800])              # the 8x12 glyph set at the segment start
    return lines, font


def build_oldies_scene(mem, *, page):
    """Compose the oldies/credits scene: black background + the recovered credit text."""
    lines, font = read_oldies(mem)
    planes = [bytearray(0x10000) for _ in range(4)]
    render_oldies(planes, lines, page, font)
    bg = RecoveredBackground(tuple(bytes(0x10000) for _ in range(4)))
    # the text is the "overlay"; compose over black so the compositor reports a COMPLETE recovered scene
    return compose_scene(bg, [lambda pl, pg: render_oldies(pl, lines, pg, font)], page)
