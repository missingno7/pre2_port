"""Bridge: lift the foreground-tile pass state (1030:3721) from VM memory. Layout only.

The pass runs with ds=1A0F. The tile-index grid is in the tilemap segment word[0x2DDA]; the tile graphics
are in segment word[0x003b] at word[0x8167 + tile*2] << 7; the foreground-flag table is [0x805E..].
"""
from __future__ import annotations

from typing import List, Tuple

from pre2.recovered.foreground_tiles import ForegroundState

_DATA = 0x1A0F
_LIST_BASE = 0x4F0A
_LIST_END = 0x5732
_LIST_STRIDE = 0x12
_NUM_TILES = 256


def _r16(d, seg, off):
    a = ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF
    return d[a] | (d[(a + 1) & 0xFFFFF] << 8)


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def read_foreground_state(mem) -> ForegroundState:
    d = mem.data
    sprites: List[Tuple[int, int, int]] = []
    for off in range(_LIST_BASE, _LIST_END, _LIST_STRIDE):
        sid = _r16(d, _DATA, off + 4)
        x = _s16(_r16(d, _DATA, off))
        y = _s16(_r16(d, _DATA, off + 2))
        sprites.append((x, y, sid))

    grid_seg = _r16(d, _DATA, 0x2DDA)
    gbase = (grid_seg << 4) & 0xFFFFF
    grid = bytes(d[gbase:gbase + 0x10000])

    fbase = ((_DATA << 4) + 0x805E) & 0xFFFFF
    flag_tbl = bytes(d[fbase:fbase + _NUM_TILES])

    gfx_index = [_r16(d, _DATA, 0x8167 + t * 2) for t in range(_NUM_TILES)]
    gfx_seg = _r16(d, _DATA, 0x003B)
    gxb = (gfx_seg << 4) & 0xFFFFF
    gfx = bytes(d[gxb:gxb + 0x10000])

    return ForegroundState(
        sprites=sprites,
        grid=grid,
        flag_tbl=flag_tbl,
        gfx=gfx,
        gfx_index=gfx_index,
        cam_col=d[(_DATA << 4) + 0x2DE4],
        cam_row=d[(_DATA << 4) + 0x2DE6],
        page=_r16(d, _DATA, 0x2DD8),
        y_bias=d[(_DATA << 4) + 0x6BC4],
    )
