"""Tests for the windowed scroll-copy (pre2.recovered.scene_scroll.window_scroll_copy).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_gameover_scroll.py: Δ=0 over 8 frames
of the game-over diorama present). These lock the window geometry: the scrolled source offset, the
0x1B80-byte (176-row) extent, all four planes copied, and the dest-page targeting."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.scene_scroll import window_scroll_copy  # noqa: E402


def _planes(fill=0):
    return [bytearray([fill]) * 0x10000 for _ in range(4)]


def test_copies_scrolled_window_all_planes():
    src = [bytearray(0x10000) for _ in range(4)]
    # mark a distinct value per plane at the scrolled source start (src_base=0, scroll=2 -> off 0x50)
    for p in range(4):
        src[p][0x50] = 0x10 + p
        src[p][0x50 + 0x1B7F] = 0xA0 + p          # last byte of the window
    dst = _planes(0xEE)
    window_scroll_copy(dst, src, scroll=2, dest_page=0x2000, src_base=0)
    for p in range(4):
        assert dst[p][0x2000] == 0x10 + p          # first window byte
        assert dst[p][0x2000 + 0x1B7F] == 0xA0 + p  # last window byte (0x1B80-byte extent)
        assert dst[p][0x2000 + 0x1B80] == 0xEE      # one past the window: untouched


def test_staging_base_offset():
    # the game-over default src_base = 0x3F40; scroll s -> source starts at 0x3F40 + 0x28*s
    src = [bytearray(0x10000) for _ in range(4)]
    s = 5
    off = 0x3F40 + 0x28 * s
    src[0][off] = 0x7B
    dst = _planes()
    window_scroll_copy(dst, src, scroll=s, dest_page=0)
    assert dst[0][0] == 0x7B


def test_dest_page_wraps_16bit():
    src = [bytearray(0x10000) for _ in range(4)]
    src[1][0x3F40] = 0x42                            # scroll 0 -> source 0x3F40
    dst = _planes()
    window_scroll_copy(dst, src, scroll=0, dest_page=0xFFFF)   # first write at 0xFFFF, then wraps to 0
    assert dst[1][0xFFFF] == 0x42
    assert dst[1][0] == src[1][0x3F41]               # next byte wrapped to offset 0
