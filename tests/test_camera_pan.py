"""Tests for the idle look-around camera pan (1030:3414/3435 -> 3588/350C).

The full byte-exact proof is the offline anim13 oracle (artifacts/anim13_witness/ +
scratchpad verify_anim13_full.py: player_fsm_step + apply_camera_pan reproduce the DS state + all 4 EGA planes
of the anim13 frame, 0 real mismatches vs the ASM). These guard the recovered logic in the committed suite.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.recovered.frame_renderer import RowFlags, calc_scroll_source, draw_tile_column


@dataclass
class _TileMap:
    tiles: bytes
    plane_attr: bytes
    tile_flags: bytes
    tile_type: bytes


def _opaque_tilemap():
    # tile id == itself everywhere; type-0 (opaque) blit; distinct OR-flag bits per the column's tiles
    tiles = bytes(range(256)) * 0x100              # 64 KiB map: tiles[si] = si & 0xFF
    plane_attr = bytes(((i & 1) for i in range(256)))     # OR -> 1 when any odd tile drawn
    tile_flags = bytes(((i & 0x10) for i in range(256)))
    tile_type = bytes((0 for i in range(256)))            # all opaque (type 0)
    return _TileMap(tiles, plane_attr, tile_flags, tile_type)


def test_draw_tile_column_stride_wrap_and_flags():
    tm = _opaque_tilemap()
    blit_type = bytes(256)                          # every tile type 0 (opaque)
    planes = [bytearray(0x10000) for _ in range(4)]
    # mark the sprite cache so the opaque blit copies a recognisable value
    for p in planes:
        for k in range(0x5E80, 0x8000):
            p[k] = 0xAB
    flags = RowFlags()
    cell = 0x1E57          # the witnessed left-pan camera cell
    bg_ptr, flags = draw_tile_column(planes, tm, cell, 0, 0x4E4E, 0x07, blit_type, b"", flags)

    # [0x2DF6]: start 0x7E80, +0x40 per row x 12 = 0x8180
    assert bg_ptr == 0x8180
    # OR-flags accumulate over the 12 column tiles (si = cell + r*0x100; tile = si & 0xFF)
    or_attr = 0
    or_flags = 0
    for r in range(12):
        t = (cell + r * 0x100) & 0xFF
        or_attr |= tm.plane_attr[t]
        or_flags |= tm.tile_flags[t]
    assert flags.plane_attr == or_attr
    assert flags.tile_flags == or_flags
    assert flags.tile_type == 0

    # the row-6 screen wrap: di starts 0x4E4E, stride 0x280 -> row6 enters 0x5D4E (>= 0x5D40) -> 0x3F4E
    assert planes[0][0x3F4E] == 0xAB        # the wrapped row drew at 0x3F4E (proves stride 0x280 + wrap)
    assert planes[0][0x4E4E] == 0xAB        # row 0


def test_calc_scroll_source_matches_witness():
    # [0x2DBA] = 2*col + 0x280*row + 0x3F40; the witnessed pan had col=[0x2DE8]=7, row=[0x2DEA]=6 -> 0x4E4E
    assert calc_scroll_source(0x07, 0x06) == 0x4E4E


class _Mem:
    def __init__(self, data):
        self.data = data


def _mem_with(camera_x, de8, dea):
    data = bytearray(0x140000)        # 1 MiB + the 4-plane EGA shadow aperture
    base = 0x1A0F << 4
    data[base + 0x2DE4] = camera_x & 0xFF
    data[base + 0x2DE4 + 1] = (camera_x >> 8) & 0xFF
    data[base + 0x2DE8] = de8
    data[base + 0x2DEA] = dea
    return _Mem(data)


def test_apply_camera_pan_left_state():
    from pre2.bridge.camera_pan import apply_camera_pan
    mem = _mem_with(camera_x=0x58, de8=0x08, dea=0x06)
    assert apply_camera_pan(mem, "left") is True
    base = 0x1A0F << 4
    assert mem.data[base + 0x2DE4] == 0x57                 # camera X decremented
    assert mem.data[base + 0x2DE8] == 0x07                 # ring column decremented
    dba = mem.data[base + 0x2DBA] | (mem.data[base + 0x2DBA + 1] << 8)
    assert dba == calc_scroll_source(0x07, 0x06)           # scroll source recomputed


def test_apply_camera_pan_left_at_edge_is_noop():
    from pre2.bridge.camera_pan import apply_camera_pan
    mem = _mem_with(camera_x=0x00, de8=0x05, dea=0x06)     # already at the left map edge
    assert apply_camera_pan(mem, "left") is False
    base = 0x1A0F << 4
    assert mem.data[base + 0x2DE4] == 0x00                 # unchanged
