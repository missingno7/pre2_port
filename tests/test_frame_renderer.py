"""Pure unit tests for the recovered tile-row draw (346E).

Byte-exact pixel fidelity is covered in-VM by pre2/probes/verify_frame.py (lockstep
vs ASM). These fast tests guard the row-loop logic itself: flag accumulation, tile
count, and the opaque-blit composition, on synthetic planes/tilemap.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.bridge.frame import TILEMAP_STRIDE, TileMap  # noqa: E402
from pre2.recovered.frame_renderer import RowFlags, draw_tile_row  # noqa: E402
from pre2.recovered.renderer import CACHE_BASE, SLOT_BYTES  # noqa: E402

PLANE = 0x10000


def _planes():
    return [bytearray(PLANE) for _ in range(4)]


def _tilemap(row_tiles, plane_attr, tile_flags, tile_type):
    tiles = bytearray(TILEMAP_STRIDE)
    tiles[: len(row_tiles)] = bytes(row_tiles)
    return TileMap(
        segment=0x1000, stride=TILEMAP_STRIDE, rows=1, tiles=bytes(tiles),
        plane_attr=bytes(plane_attr), tile_flags=bytes(tile_flags), tile_type=bytes(tile_type),
    )


def test_flags_are_or_accumulated_over_the_row():
    row = list(range(20))  # tile indices 0..19
    plane_attr = bytes((i & 0x0F) for i in range(256))
    tile_flags = bytes((i & 0x30) for i in range(256))
    tile_type = bytes(0 for _ in range(256))  # all opaque -> simple blit
    tm = _tilemap(row, plane_attr, tile_flags, tile_type)
    blit_type = bytes(256)  # all type 0
    mask_region = bytes(0x2000)

    _di, flags = draw_tile_row(_planes(), tm, tile_offset=0, di=0, scroll_src=0,
                               col_ring=0, fine_scroll=0, blit_type=blit_type,
                               mask_region=mask_region)
    expected_attr = 0
    expected_flags = 0
    for t in row:
        expected_attr |= plane_attr[t]
        expected_flags |= tile_flags[t]
    assert flags.plane_attr == expected_attr
    assert flags.tile_flags == expected_flags
    assert flags.tile_type == 0


def test_seed_flags_are_preserved_in_or():
    tm = _tilemap([0] * 20, bytes(256), bytes(256), bytes(256))
    seed = RowFlags(plane_attr=0x80, tile_flags=0x40, tile_type=0x20)
    _di, flags = draw_tile_row(_planes(), tm, 0, 0, 0, 0, 0, bytes(256), bytes(0x2000), seed)
    assert flags.plane_attr == 0x80 and flags.tile_flags == 0x40 and flags.tile_type == 0x20


def test_opaque_blit_copies_cache_to_screen():
    # one opaque tile (index 7) at di=0; opaque blit copies cache slot -> screen.
    planes = _planes()
    idx = 7
    src = CACHE_BASE + idx * SLOT_BYTES
    for p in range(4):
        for k in range(SLOT_BYTES):
            planes[p][src + k] = (p * 0x40 + k) & 0xFF
    tm = _tilemap([idx] + [0] * 19, bytes(256), bytes(256), bytes(256))
    draw_tile_row(planes, tm, 0, 0, 0, 0, 0, bytes(256), bytes(0x2000))
    # first sprite row (2 bytes) of tile 0 lands at screen offset 0
    for p in range(4):
        assert planes[p][0] == planes[p][src + 0]
        assert planes[p][1] == planes[p][src + 1]
