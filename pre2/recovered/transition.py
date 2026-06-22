"""Prehistorik 2 screen-transition primitives — recovered native logic (pure).

The end-level effect (`1030:31D0` loop) zooms/scales the screen down over several
frames while fading the palette. This module recovers its renderer primitives, one
bounded routine at a time, so the transition's pixel work becomes clean recovered
source (the multi-frame loop that *drives* them is a thin controller).

First primitive recovered here:

* :func:`clear_span` (``1030:32DE``) — clear a horizontal pixel span across all four
  EGA planes, with partial-byte edge masks. Used to wipe the borders exposed as the
  image shrinks.

Pure: no ``cpu``/``mem``/``dos_re`` imports. Plane buffers + the dest page/stride are
passed in; the VM↔memory translation lives in ``pre2/bridge/``.
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = ["SCREEN_W", "SCREEN_H", "ROW_STRIDE", "clear_span"]

SCREEN_W = 0x140        # 320 px  [asm 32E3: cmp bx,0x140]
SCREEN_H = 0xC8         # 200 rows [asm 32EF: cmp dx,0xC8]
ROW_STRIDE = 0x28       # 40 bytes per row


@oracle_link("1030:32DE",
             "clear a horizontal pixel span [x, x+width) at screen row `row` across all "
             "4 EGA planes (partial-byte edge masks at both ends); VRAM byte = "
             "row*0x28 + page + x>>3",
             "ASM_MATCHED", merge_target="frame renderer")
def clear_span(planes, x: int, width: int, row: int, page: int,
               stride: int = ROW_STRIDE) -> None:
    """Recover ``1030:32DE`` — clear pixels ``[x, x+width)`` at ``row`` (all 4 planes).

    ``planes`` is the four EGA plane buffers; the caller has selected SC map mask 0x0F
    so every write hits all planes. No-op if out of bounds (matching the ASM guards).
    """
    if x >= SCREEN_W or width > SCREEN_W or row >= SCREEN_H:   # [asm 32E3..32F3]
        return
    di = (row * stride + page + (x >> 3)) & 0xFFFF             # [asm 32F5..3305]
    x_sub = x & 7
    if x_sub != 0 or width >= 8:                               # [asm 3308 jne / 330D cmp 8,jb]
        # left partial: keep the bits before x in this byte, clear from x to byte end.
        keep = (~(0xFF >> x_sub)) & 0xFF                       # [asm 331E-3322: not(0xFF>>cl)]
        for p in range(4):
            planes[p][di] &= keep                              # GC AND, map mask 0x0F
        di = (di + 1) & 0xFFFF                                 # [asm 3328]
        total = (width + x_sub) & 0xFFFF                       # [asm 3334-3337: cx += x&7]
        full = total >> 3                                      # [asm 333C-3340]
        for _ in range(full - 1 if full else 0):               # [asm 3342 je / 3344 dec cx / rep stosb]
            for p in range(4):
                planes[p][di] = 0
            di = (di + 1) & 0xFFFF
        cl = total & 7                                         # [asm 3349 pop cx / 334A and cl,7]
    else:                                                      # aligned + width<8 -> right partial only
        cl = width & 7
    if cl != 0:                                                # [asm 334D je / 334F-335A]
        right_keep = (0xFF >> cl) & 0xFF
        for p in range(4):
            planes[p][di] &= right_keep
