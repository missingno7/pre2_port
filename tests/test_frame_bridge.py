"""Unit tests for the frame-renderer / scroll-engine memory bridge.

Pure (no VM): writes known values at the documented data-segment offsets and
checks pre2.bridge.frame parses the Camera/ScrollState contract — including the
witness-confirmed ring invariant and the 0x55AA dirty sentinel.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.bridge import frame as F  # noqa: E402


class _FakeMem:
    """Minimal flat-memory stand-in exposing the ``.data`` bytearray the bridge uses."""

    def __init__(self) -> None:
        self.data = bytearray(0x30000)

    def wb(self, off: int, val: int) -> None:
        self.data[((F.DATA_SEG << 4) + off) & 0xFFFFF] = val & 0xFF

    def ww(self, off: int, val: int) -> None:
        base = ((F.DATA_SEG << 4) + off) & 0xFFFFF
        self.data[base] = val & 0xFF
        self.data[base + 1] = (val >> 8) & 0xFF


def _seed(mem, *, x, y, prev_x, prev_y, dirty_rows=0):
    mem.ww(F.VAR_CAMERA_X, x)
    mem.ww(F.VAR_CAMERA_Y, y)
    mem.ww(F.VAR_PREV_CAMERA_X, prev_x)
    mem.ww(F.VAR_PREV_CAMERA_Y, prev_y)
    mem.wb(F.VAR_COL_RING, x % F.RING_COLS)
    mem.wb(F.VAR_ROW_RING, y % F.RING_ROWS)
    mem.wb(F.VAR_FINE_SCROLL, 0)
    mem.ww(F.VAR_SCROLL_SRC, 0x55C0)
    mem.wb(F.VAR_ROW_FACTOR, 0)
    mem.ww(F.VAR_DEST_PAGE_A, 0x2000)
    mem.ww(F.VAR_DEST_PAGE_B, 0x0000)
    mem.ww(F.VAR_SHEET_SEG, 0xD4C5)
    mem.wb(F.VAR_LEVEL_HEIGHT, 49)
    mem.wb(F.VAR_DIRTY, 1)
    mem.wb(F.VAR_DIRTY_ROWS, dirty_rows)


def test_read_scroll_state_parses_contract():
    mem = _FakeMem()
    _seed(mem, x=0, y=33, prev_x=0, prev_y=33)
    st = F.read_scroll_state(mem)
    assert (st.camera.x, st.camera.y) == (0, 33)
    assert st.sheet_seg == 0xD4C5
    assert st.level_height == 49
    assert (st.dest_page_a, st.dest_page_b) == (0x2000, 0x0000)
    assert st.scroll_src == 0x55C0


def test_ring_invariant_matches_witness():
    # demo 091827 panned camera_y 0->0x21; row_ring tracked y % 12 exactly.
    mem = _FakeMem()
    for y in (0x00, 0x14, 0x1A, 0x1F, 0x21):
        _seed(mem, x=0, y=y, prev_x=0, prev_y=y)
        cam = F.read_camera(mem)
        assert cam.row_ring == y % F.RING_ROWS
        assert cam.col_ring == 0


def test_tilemap_row_major_indexing():
    # 346E reads tile index via lodsb at offset row*stride + col.
    mem = _FakeMem()
    seg = 0x1000  # arbitrary; kept small so flat addr fits the fake buffer
    mem.ww(F.VAR_LEVEL_SEG, seg)
    mem.wb(F.VAR_LEVEL_HEIGHT, 49)
    flat = (seg << 4) & 0xFFFFF
    # plant the witnessed row 33 (si = 33*0x100) into the level block
    witness = bytes.fromhex("21 44 6B 21 44 1D 1E 46 7E 7E 7E 7E 7E 7E 7E 7E 7E 7E DD 46".replace(" ", ""))
    mem.data[flat + 33 * F.TILEMAP_STRIDE: flat + 33 * F.TILEMAP_STRIDE + len(witness)] = witness

    # plant a byte in the bottom-fill row (row == level_height, one past the last
    # 0-indexed row) — the camera-at-bottom over-scroll case that crashed.
    mem.data[flat + 49 * F.TILEMAP_STRIDE + 0x3F] = 0xC3

    tm = F.read_tilemap(mem)
    assert tm.segment == seg
    assert tm.stride == F.TILEMAP_STRIDE
    assert tm.rows == 49                              # level height (informational)
    assert len(tm.tiles) >= (49 + 1) * F.TILEMAP_STRIDE  # window covers past the last row
    assert tm.row_slice(0, 33, 20) == witness
    assert tm.tile(0, 33) == 0x21
    assert tm.tile(2, 33) == 0x6B
    assert tm.tile(8, 33) == 0x7E  # sky/empty tile
    # the over-scroll bottom-fill tile (si = 49*256 + 0x3F = 0x313F) is readable, not OOB
    assert tm.tile(0x3F, 49) == 0xC3


def test_moved_and_dirty_sentinel():
    mem = _FakeMem()
    _seed(mem, x=5, y=33, prev_x=5, prev_y=33)
    assert F.read_camera(mem).moved is False
    # camera advanced one column
    _seed(mem, x=6, y=33, prev_x=5, prev_y=33)
    assert F.read_camera(mem).moved is True
    # 3B40's full-redraw sentinel in prev_x also reads as "moved"
    _seed(mem, x=5, y=33, prev_x=F.DIRTY_SENTINEL, prev_y=33)
    assert F.read_camera(mem).moved is True
