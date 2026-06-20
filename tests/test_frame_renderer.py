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
from pre2.recovered.frame_renderer import RowFlags, draw_grid, draw_tile_row  # noqa: E402
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


# ---- draw_grid (3582) -------------------------------------------------------

def _grid_tilemap(rows=24, type_map=None, flags_map=None):
    """A TileMap whose tile index == its type/flags (tile i -> type_map[i])."""
    tiles = bytes((r * 7 + c) % 4 for r in range(rows) for c in range(TILEMAP_STRIDE))
    tt = bytes(type_map or [i & 0x03 for i in range(256)])      # type table (1A13:0x4DF4)
    tf = bytes(flags_map or [(i * 3) & 0xFF for i in range(256)])  # tile-flags (1A13:0x805A)
    return TileMap(segment=0x1000, stride=TILEMAP_STRIDE, rows=rows, tiles=tiles,
                   plane_attr=bytes(256), tile_flags=tf, tile_type=tt)


def _call_grid(tm, *, cam_x, cam_y, prev_x, prev_y, dirty, dirty_rows, blit_type=None):
    return draw_grid(_planes(), tm, cam_x, cam_y, prev_x, prev_y, dirty, dirty_rows,
                     scroll_src=0x3F40, col_ring=0, fine_scroll=0,
                     blit_type=blit_type if blit_type is not None else tm.tile_type,
                     mask_region=bytes(0x2000))


def test_grid_exits_when_static_and_clean():
    tm = _grid_tilemap()
    r = _call_grid(tm, cam_x=5, cam_y=3, prev_x=5, prev_y=3, dirty=0, dirty_rows=0)
    assert r.redrew is False
    assert (r.prev_x, r.prev_y) == (5, 3)  # prev updated to camera on the clean path


def test_grid_redraws_when_rows_scrolled_without_touching_prev():
    tm = _grid_tilemap()
    r = _call_grid(tm, cam_x=5, cam_y=3, prev_x=1, prev_y=2, dirty=0, dirty_rows=4)
    assert r.redrew is True
    assert (r.prev_x, r.prev_y) == (1, 2)  # 3590 path leaves prev unchanged


def test_grid_camera_change_alone_does_not_redraw():
    # 35A1->35B2: camera moved but dirty==0 -> exit (matches ASM); prev_x updated,
    # prev_y left (we jumped before its store).
    tm = _grid_tilemap()
    r = _call_grid(tm, cam_x=9, cam_y=3, prev_x=5, prev_y=3, dirty=0, dirty_rows=0)
    assert r.redrew is False
    assert r.prev_x == 9 and r.prev_y == 3


def test_grid_redraw_accumulates_flags_and_sets_dirty_only_for_type_ge_1():
    # all tiles type 0 -> redraw runs but nothing blits -> dirty stays 0.
    tm0 = _grid_tilemap(type_map=[0] * 256)
    r0 = _call_grid(tm0, cam_x=0, cam_y=0, prev_x=0, prev_y=0, dirty=1, dirty_rows=1)
    assert r0.redrew is True and r0.dirty == 0 and r0.dirty_rows == 0
    # tile_flags is the OR over every visited tile index's flag-table entry
    exp = 0
    for row in range(12):
        for col in range(20):
            exp |= tm0.tile_flags[tm0.tiles[row * 0x100 + col]]
    assert r0.tile_flags == (exp & 0xFF)
    # a type>=1 somewhere -> dirty becomes 1
    tt = [0] * 256
    tt[tm0.tiles[0]] = 2
    r1 = _call_grid(tm0, cam_x=0, cam_y=0, prev_x=0, prev_y=0, dirty=1, dirty_rows=1, blit_type=bytes(tt))
    assert r1.dirty == 1
