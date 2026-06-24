"""The foreground-tile pass (1030:3721 selection + 1030:37F7 masked blit).

Some map tiles (flag bit **0x40** in the tile-flag table `[0x805E + tile]`) must draw IN FRONT of the
moving sprites (a bush the player walks behind, a pillar edge, …). The object pass (26FA) draws sprites
over the background — including over those foreground tiles — so a SEPARATE pass after it redraws the
foreground tiles back over the sprites that overlap them.

`3721`/`3732` is the **selection**: walk the active sprite list (`0x4F0A`, stride 0x12, to `0x5732`);
for each active entry (id != 0xFFFF and id & 0x2000) take the sprite's tile cell and scan a box of cells
around it (4 columns × 3-4 rows, growing one row when the sprite Y is sub-tile). For each cell whose tile
has flag bit 0x40, call `37F7` to redraw that tile. The grid of tile indices is read from the tilemap
segment `[0x2DDA]` (`es:[di]`, di = row*0x100 + col).

`37F7` is the **blit** — a color-0-keyed transparent tile blit, proven byte-exact vs the ASM (diff=0 on
five tiles in snapshot 110346):
  * graphic = 128 bytes at `seg [0x003b] : (word[0x8167 + tile*2] << 7)`, laid out plane-major
    (plane p, row r -> offset p*0x20 + r*2, a 16-bit row word, 16 px wide, 16 rows).
  * phase 1 [asm 385F]: AND `~footprint` into all 4 planes (footprint = OR of the 4 plane rows) — punch
    a transparent hole where the tile is opaque.
  * phase 2 [asm 389C]: OR each plane's row word back in — fill the tile color.
  net per pixel: opaque tile pixels (any plane bit set) replace the destination; all-zero pixels are
  transparent (the sprite shows through).

The destination cell -> screen offset [asm 3800] mirrors the other tile blits: screen_row =
(cell>>8) - cam_row, screen_col = (cell & 0xff) - cam_col, di = screen_row*0x280 + screen_col*2 + page
- 0x28*y_bias.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Sequence, Tuple

from pre2.islands import oracle_link

_LIST_BASE = 0x4F0A
_LIST_END = 0x5732
_LIST_STRIDE = 0x12
_FG_FLAG = 0x40
_PLANE_STRIDE = 0x20    # tile graphic: 0x20 bytes per plane (16 rows * 2)


@dataclass
class ForegroundState:
    """Everything the foreground pass needs, lifted from VM memory by the bridge."""
    sprites: List[Tuple[int, int, int]]   # active list entries (x, y, id) — signed x/y
    grid: bytes                           # tile-index grid (tilemap seg [0x2DDA]), indexed by cell
    flag_tbl: bytes                       # [0x805E..]; flag_tbl[tile] & 0x40 -> foreground
    gfx: bytes                            # the tile-graphic segment ([0x003b]) as a byte block
    gfx_index: Sequence[int]              # word[0x8167 + tile*2] per tile (graphic offset >> 7)
    cam_col: int                          # [0x2DE4] low byte
    cam_row: int                          # [0x2DE6] low byte
    page: int                             # [0x2DD8]
    y_bias: int                           # [0x6BC4]


def select_foreground_cells(fg: ForegroundState) -> Iterator[Tuple[int, int]]:
    """Yield (tile, cell) for every foreground tile 3721/3732 would redraw, in ASM order."""
    grid = fg.grid
    flag = fg.flag_tbl
    cam_row = fg.cam_row & 0xFF
    cam_col = fg.cam_col & 0xFF
    for x, y, sid in fg.sprites:
        if sid == 0xFFFF:
            continue
        if not (sid & 0x2000):            # bh & 0x20 -> bit 13 of the id word [asm 3747]
            continue
        # base cell = (tile_row - 1)*0x100 + tile_col   [asm 3751-375D]
        row = ((y >> 4) - 1) & 0xFF
        col = (x >> 4) & 0xFF
        di = (row << 8) | col
        sch = (row - cam_row) & 0xFF      # ch = screen tile row
        scl = (col - cam_col) & 0xFF      # cl = screen tile col (signed via 0x80 wrap below)
        scl = scl - 0x100 if scl >= 0x80 else scl
        rows = 3
        if y & 0xF:                       # sub-tile Y -> one extra row, shifted down [asm 376C]
            di = (di + 0x100) & 0xFFFF
            sch = (sch + 1) & 0xFF
            rows += 1
        for _ in range(rows):
            if sch >= 0xB:                # off the bottom of the viewport [asm 3779]
                di = (di - 0x100) & 0xFFFF
                continue
            sch = (sch - 1) & 0xFF        # [asm 377E]
            # the four horizontal neighbours, each gated by the screen column [asm 3782..37E0]
            if scl >= 2:
                yield from _maybe(grid, flag, (di - 2) & 0xFFFF)
            if scl >= 1:
                yield from _maybe(grid, flag, (di - 1) & 0xFFFF)
            if 0 <= scl < 0x14:
                yield from _maybe(grid, flag, di)
            if -1 <= scl < 0x13:
                yield from _maybe(grid, flag, (di + 1) & 0xFFFF)
            di = (di - 0x100) & 0xFFFF


def _maybe(grid: bytes, flag: bytes, cell: int):
    tile = grid[cell]
    if flag[tile] & _FG_FLAG:
        yield tile, cell


@oracle_link("1030:3721",
             "foreground-tile pass: redraw flag-0x40 tiles (table [0x805E]) over the sprites the object "
             "pass drew on top of them. Walk the active list [0x4F0A] (stride 0x12); for each active "
             "sprite (id!=0xFFFF & id&0x2000) scan a 4-col x 3-4-row cell box and, per flag-0x40 cell, "
             "blit the tile (37F7) as a color-0-keyed transparent tile: phase1 AND ~footprint, phase2 OR "
             "the tile color, per plane. Graphic at seg[0x003b]:(word[0x8167+tile*2]<<7), plane-major.",
             "VERIFIED", merge_target="render_frame")
def render_foreground_tiles(planes: Sequence[bytearray], fg: ForegroundState) -> None:
    """Apply the foreground pass onto ``planes`` (4 EGA planes) — recover 3721 + 37F7 together."""
    for tile, cell in select_foreground_cells(fg):
        _blit_tile(planes, tile, cell, fg)


def _blit_tile(planes: Sequence[bytearray], tile: int, cell: int, fg: ForegroundState) -> None:
    gfx_off = (fg.gfx_index[tile] << 7) & 0xFFFF
    screen_row = ((cell >> 8) - (fg.cam_row & 0xFF)) & 0xFF
    screen_col = ((cell & 0xFF) - (fg.cam_col & 0xFF)) & 0xFFFF
    di = ((screen_row * 0x280) + (screen_col * 2) + fg.page) & 0xFFFF
    di = (di - 0x28 * fg.y_bias) & 0xFFFF
    gfx = fg.gfx
    for r in range(16):
        rowbase = (gfx_off + r * 2) & 0xFFFF
        pl = [gfx[(rowbase + p * _PLANE_STRIDE) & 0xFFFF] | (gfx[(rowbase + p * _PLANE_STRIDE + 1) & 0xFFFF] << 8)
              for p in range(4)]
        mask = pl[0] | pl[1] | pl[2] | pl[3]      # opaque footprint
        keep = (~mask) & 0xFFFF
        o, o2 = di & 0xFFFF, (di + 1) & 0xFFFF
        for p in range(4):
            cur = planes[p][o] | (planes[p][o2] << 8)
            val = (cur & keep) | pl[p]
            planes[p][o] = val & 0xFF
            planes[p][o2] = (val >> 8) & 0xFF
        di = (di + 0x28) & 0xFFFF
