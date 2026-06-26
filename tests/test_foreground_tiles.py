"""Pure-logic tests for the foreground-tile pass (pre2.recovered.foreground_tiles).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_foreground_tiles.py, Δ=0 over 5 passes
on snapshot 110346); these lock the selection geometry (active-bit gate, dead-sprite skip, the flag-0x40
cell box) and the masked-blit mechanics (color-0 transparency: opaque pixels replace, zero pixels keep)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.foreground_tiles import (  # noqa: E402
    ForegroundState, render_foreground_tiles, select_foreground_cells)


def _state(sprites, grid_pairs, flag_pairs, gfx=None, gfx_index=None,
           cam_col=0, cam_row=0, page=0, y_bias=0):
    grid = bytearray(0x10000)
    for cell, tile in grid_pairs:
        grid[cell] = tile
    flag = bytearray(256)
    for tile, f in flag_pairs:
        flag[tile] = f
    return ForegroundState(
        sprites=sprites, grid=bytes(grid), flag_tbl=bytes(flag),
        gfx=gfx if gfx is not None else bytes(0x10000),
        gfx_index=gfx_index if gfx_index is not None else [0] * 256,
        cam_col=cam_col, cam_row=cam_row, page=page, y_bias=y_bias)


def test_selection_picks_flag_0x40_cell():
    # sprite at tile (row=5,col=10), tile-aligned -> base row 4. The cell box covers di=0x040A.
    fg = _state(sprites=[(10 * 16, 5 * 16, 0x2000)],
                grid_pairs=[(0x040A, 7)], flag_pairs=[(7, 0x40)])
    cells = list(select_foreground_cells(fg))
    assert (7, 0x040A) in cells
    assert all(tile == 7 for tile, _ in cells)   # only the flagged tile


def test_non_foreground_tile_not_selected():
    fg = _state(sprites=[(10 * 16, 5 * 16, 0x2000)],
                grid_pairs=[(0x040A, 7)], flag_pairs=[(7, 0x00)])   # flag bit clear
    assert list(select_foreground_cells(fg)) == []


def test_inactive_and_dead_sprites_skipped():
    # id without bit 0x2000 -> skipped; id 0xFFFF -> skipped
    fg = _state(sprites=[(10 * 16, 5 * 16, 0x0000), (10 * 16, 5 * 16, 0xFFFF)],
                grid_pairs=[(0x040A, 7)], flag_pairs=[(7, 0x40)])
    assert list(select_foreground_cells(fg)) == []


def test_masked_blit_opaque_replaces_transparent_keeps():
    # tile 5, gfx_off 0. Row 0: plane0 word = 0x00FF (low byte opaque), others 0 -> color 1 there.
    gfx = bytearray(0x10000)
    gfx[0] = 0xFF        # plane0 row0 low byte
    gfx[1] = 0x00        # plane0 row0 high byte
    fg = _state(sprites=[], grid_pairs=[], flag_pairs=[],
                gfx=bytes(gfx), gfx_index=[0] * 256, page=0)
    planes = [bytearray(0x10000) for _ in range(4)]
    # pre-fill the destination word so we can see keep-vs-replace. The word at di spans byte di (low,
    # the opaque footprint here) and di+1 (high, transparent).
    di = 0                                   # screen (0,0), page 0, no y_bias
    for p in range(4):
        planes[p][di] = 0x55                 # opaque-footprint byte (replaced)
        planes[p][di + 1] = 0xAA             # transparent byte (kept)
    from pre2.recovered.foreground_tiles import _blit_tile
    _blit_tile(planes, tile=5, cell=0x0000, fg=fg)
    # byte di is the fully-opaque footprint -> plane0 = 0xFF, planes1-3 = 0x00 (color 1)
    assert planes[0][di] == 0xFF
    assert planes[1][di] == 0x00 and planes[2][di] == 0x00 and planes[3][di] == 0x00
    # byte di+1 is transparent (mask 0) -> destination unchanged
    for p in range(4):
        assert planes[p][di + 1] == 0xAA


def test_render_runs_full_pass_without_error():
    fg = _state(sprites=[(10 * 16, 5 * 16, 0x2000)],
                grid_pairs=[(0x040A, 7)], flag_pairs=[(7, 0x40)],
                gfx_index=[0] * 256)
    planes = [bytearray(0x10000) for _ in range(4)]
    render_foreground_tiles(planes, fg)      # exercises selection -> blit end to end


def test_blit_clips_offscreen_destination():
    # [asm 3835-3846] the blit only draws when page <= di < page+0x1900; a tile whose top falls outside
    # that window is SKIPPED (without this it bleeds off-screen / into the HUD band — demo 144815 bug).
    from pre2.recovered.foreground_tiles import _blit_tile
    gfx = bytearray(0x10000); gfx[0] = 0xFF
    fg = _state(sprites=[], grid_pairs=[], flag_pairs=[], gfx=bytes(gfx), gfx_index=[0] * 256, page=0)
    planes = [bytearray(0x10000) for _ in range(4)]
    # cell row 10 -> di = 10*0x280 = 0x1900 == page+0x1900 -> clipped (window is half-open)
    assert _blit_tile(planes, tile=5, cell=0x0A00, fg=fg) is False
    assert not any(any(p) for p in planes)          # nothing drawn off-screen
    # cell row 9 -> di = 0x1680, inside the window -> blits
    assert _blit_tile(planes, tile=5, cell=0x0900, fg=fg) is True
    assert any(any(p) for p in planes)


def test_render_count_excludes_clipped_blits():
    # the live hook uses the blit count to decide the EGA exit state; a clipped 37F7 sets no port state,
    # so render must NOT count off-screen tiles. A sprite far below the viewport selects only clipped cells.
    fg = _state(sprites=[(10 * 16, 40 * 16, 0x2000)],     # tile row 40 -> way below the viewport
                grid_pairs=[(0x270A, 7), (0x280A, 7)], flag_pairs=[(7, 0x40)], gfx_index=[0] * 256)
    planes = [bytearray(0x10000) for _ in range(4)]
    n = render_foreground_tiles(planes, fg)
    assert n == 0 and not any(any(p) for p in planes)   # all clipped -> no blits, planes untouched
