"""Memory views for the frame-renderer / scroll-engine island.

VM memory ⇄ recovered dataclasses, the one place that knows *where* the camera
and scroll bookkeeping live in PRE2 memory (data segment ``1A13``). Rendering
decisions belong in ``pre2/recovered/frame_renderer.py`` (the merge target); this
module only translates layout.

Field semantics are taken from disassembly (the "frame renderer / scroll engine"
section of ``docs/pre2/symbol_ledger.md``) and **confirmed by witness**: see
``pre2/probes/capture_frame_state.py`` + ``artifacts/frame_state_witness/``. The
witness showed, on demo 091827, ``row_ring_idx == camera_y % RING_ROWS`` exactly
as the camera panned 0→0x21, ``scroll_src`` tracking the camera off ``SCROLL_BASE``,
``[0x2DF1]`` counting tile-rows scrolled per frame, and ``[0x2DDC]`` carrying the
``0x55AA`` dirty sentinel ``3B40`` writes.
"""
from __future__ import annotations

from dataclasses import dataclass

DATA_SEG = 0x1A13

# --- visible-window / ring geometry (from the tile loops 346E/3582 and 3344/338E) -
VISIBLE_COLS = 0x14      # 20 tiles across (cl=0x14 in 346E/3582)
VISIBLE_ROWS = 0x0C      # 12 tile rows drawn (ch=0x0C in 3582)
RING_COLS = 0x14         # column ring modulus (col index wraps 0..0x13)
RING_ROWS = 0x0C         # row ring modulus (row index wraps 0..0x0B; 12)
TILE_PX = 0x10           # fine pixel scroll wraps at one tile = 16 px ([0x6BC0])
SCROLL_BASE = 0x3F40     # scroll-source ring-buffer base ([0x2DB6] computed by 3569)
DIRTY_SENTINEL = 0x55AA  # 3B40 seeds [0x2DDC] with this to force a full redraw

# --- data-segment variables (offsets within ds=1A13) -------------------------
VAR_CAMERA_X = 0x2DE0    # camera column, in tiles
VAR_CAMERA_Y = 0x2DE2    # camera row, in tiles
VAR_PREV_CAMERA_X = 0x2DDC  # previous camera X (dirty compare; also 0x55AA sentinel)
VAR_PREV_CAMERA_Y = 0x2DDE  # previous camera Y
VAR_COL_RING = 0x2DE4    # column ring index (camera_x % RING_COLS)
VAR_ROW_RING = 0x2DE6    # row ring index (camera_y % RING_ROWS)
VAR_FINE_SCROLL = 0x6BC0  # sub-tile pixel scroll (0..TILE_PX)
VAR_ROW_FACTOR = 0x6BF4  # row-stride factor (0x28 * this in 3A08/3582)
VAR_SCROLL_SRC = 0x2DB6  # scroll source offset into the ring buffer
VAR_DEST_PAGE_A = 0x2DD2  # double-buffer page offset (front/back; 0 or 0x2000)
VAR_DEST_PAGE_B = 0x2DD4  # the other double-buffer page offset
VAR_SHEET_SEG = 0x2DD6   # tilesheet segment used by the draw loops
VAR_LEVEL_HEIGHT = 0x2CF1  # level height in tile rows
VAR_DIRTY = 0x2DF0       # composite dirty flags (rebuild-grid / type seen); also the
                         # tile-type accumulator 346E ORs into ([0x2DF0])
VAR_DIRTY_ROWS = 0x2DF1  # tile-rows scrolled this frame (reset after redraw)
# the two other per-row attribute accumulators 346E ORs into:
VAR_PLANE_ATTR = 0x6BB9  # plane/attribute flags accumulator
VAR_TILE_FLAGS = 0x2DEE  # tile-flags accumulator

# --- tilemap layout (from 346E; witnessed) -----------------------------------
# The level segment [0x2DD6] holds the row-major tile map (1 byte/tile = tile
# index), the per-tile attribute tables, and (higher up) the sprite sheet. 346E
# reads the tile index with `lodsb` at offset (camera_y * TILEMAP_STRIDE +
# camera_x), confirmed by dump (row 33 = "21 44 6B 21 44 1D ... 7E 7E", 7E=sky).
VAR_LEVEL_SEG = 0x2DD6   # level-data block base segment (== sprites' VAR_LOCAL_BASE)
TILEMAP_STRIDE = 0x100   # bytes per tile row (caller passes ah=row,al=col -> si=row*256+col)
# per-tile attribute lookup tables, all in the level segment, indexed by tile index:
TBL_PLANE_ATTR = 0x6984  # xlatb -> OR into [0x6BB9] (plane/attribute flags)
TBL_TILE_FLAGS = 0x805A  # xlatb -> OR into [0x2DEE] (tile flags)
TBL_TILE_TYPE = 0x4DF4   # xlatb -> OR into [0x2DF0] (type/dirty)


def _rb(mem, off: int) -> int:
    return mem.data[((DATA_SEG << 4) + off) & 0xFFFFF]


def _rw(mem, off: int) -> int:
    base = ((DATA_SEG << 4) + off) & 0xFFFFF
    return mem.data[base] | (mem.data[base + 1] << 8)


@dataclass(frozen=True)
class Camera:
    """Camera position in tile coordinates and its ring-buffer indices."""

    x: int            # camera column (tiles), [0x2DE0]
    y: int            # camera row (tiles), [0x2DE2]
    prev_x: int       # previous-frame camera X (dirty compare), [0x2DDC]
    prev_y: int       # previous-frame camera Y, [0x2DDE]
    col_ring: int     # column ring index, [0x2DE4]  (== x % RING_COLS)
    row_ring: int     # row ring index, [0x2DE6]      (== y % RING_ROWS)
    fine_scroll: int  # sub-tile pixel offset, [0x6BC0] (0..TILE_PX)

    @property
    def moved(self) -> bool:
        """Whether the camera differs from the previous frame (forces redraw).

        Matches 3582's dirty test, including the ``0x55AA`` sentinel 3B40 writes
        into ``prev_x`` to force a full grid rebuild.
        """
        return self.x != self.prev_x or self.y != self.prev_y


@dataclass(frozen=True)
class ScrollState:
    """The full scroll-engine bookkeeping for one frame."""

    camera: Camera
    scroll_src: int    # source offset into the ring buffer, [0x2DB6]
    row_factor: int    # row-stride factor, [0x6BF4]
    dest_page_a: int   # double-buffer page offset, [0x2DD2]
    dest_page_b: int   # other page offset, [0x2DD4]
    sheet_seg: int     # tilesheet segment, [0x2DD6]
    level_height: int  # level height in tile rows, [0x2CF1]
    dirty: int         # composite dirty flags, [0x2DF0]
    dirty_rows: int    # tile-rows scrolled this frame, [0x2DF1]


@dataclass(frozen=True)
class TileMap:
    """Row-major level tile map: ``tiles[row*stride + col]`` is a tile index (uint8).

    VM-independent: holds a plain copy of the tile region plus the three per-tile
    attribute tables, so recovered draw logic never touches ``mem``.
    """

    segment: int           # level-data block segment, [0x2DD6] (holds tile indices)
    stride: int            # bytes per row (TILEMAP_STRIDE)
    rows: int              # number of tile rows held (typically level_height)
    tiles: bytes           # rows*stride bytes of tile indices, base offset 0
    # The three attribute tables live in the DATA segment 1A13 (346E's xlatb carry
    # an ES override, es=1A13), NOT the level segment. tile_type IS the same table
    # the blit dispatches on (1A13:0x4DF4).
    plane_attr: bytes      # 256-entry table: tile index -> plane/attr flags (1A13:0x6984)
    tile_flags: bytes      # 256-entry table: tile index -> tile flags (1A13:0x805A)
    tile_type: bytes       # 256-entry table: tile index -> type/dirty bits (1A13:0x4DF4)

    def tile(self, col: int, row: int) -> int:
        """Tile index at (col, row); 346E reads this as ``lodsb`` at row*stride+col."""
        return self.tiles[(row * self.stride + col) % len(self.tiles)]

    def row_slice(self, col: int, row: int, count: int) -> bytes:
        """``count`` consecutive tile indices starting at (col, row) — one draw row."""
        start = row * self.stride + col
        return self.tiles[start:start + count]


def read_row_flags(mem) -> tuple[int, int, int]:
    """The three per-row attribute accumulators 346E ORs into:
    ``(plane_attr [0x6BB9], tile_flags [0x2DEE], tile_type [0x2DF0])``."""
    return _rb(mem, VAR_PLANE_ATTR), _rb(mem, VAR_TILE_FLAGS), _rb(mem, VAR_DIRTY)


def write_row_flags(mem, plane_attr: int, tile_flags: int, tile_type: int) -> None:
    """Write the three row-flag accumulators back (the 346E write-back contract)."""
    base = (DATA_SEG << 4) & 0xFFFFF
    mem.data[base + VAR_PLANE_ATTR] = plane_attr & 0xFF
    mem.data[base + VAR_TILE_FLAGS] = tile_flags & 0xFF
    mem.data[base + VAR_DIRTY] = tile_type & 0xFF


def write_dirty_state(mem, prev_x: int, prev_y: int, *, dirty=None, dirty_rows=None,
                      tile_flags=None) -> None:
    """Write the grid-redraw side effects back (3582): prev camera always; the dirty
    flags + tile-flags accumulator only when provided (i.e. only on an actual redraw)."""
    mem.ww(DATA_SEG, VAR_PREV_CAMERA_X, prev_x & 0xFFFF)
    mem.ww(DATA_SEG, VAR_PREV_CAMERA_Y, prev_y & 0xFFFF)
    base = (DATA_SEG << 4) & 0xFFFFF
    if tile_flags is not None:
        mem.data[base + VAR_TILE_FLAGS] = tile_flags & 0xFF
    if dirty is not None:
        mem.data[base + VAR_DIRTY] = dirty & 0xFF
    if dirty_rows is not None:
        mem.data[base + VAR_DIRTY_ROWS] = dirty_rows & 0xFF


def read_camera(mem) -> Camera:
    return Camera(
        x=_rw(mem, VAR_CAMERA_X),
        y=_rw(mem, VAR_CAMERA_Y),
        prev_x=_rw(mem, VAR_PREV_CAMERA_X),
        prev_y=_rw(mem, VAR_PREV_CAMERA_Y),
        col_ring=_rb(mem, VAR_COL_RING),
        row_ring=_rb(mem, VAR_ROW_RING),
        fine_scroll=_rb(mem, VAR_FINE_SCROLL),
    )


# Blit input tables (1A13 domain), needed because the row draw composes the
# recovered blit. Masks occupy [0x2DF4 .. 0x4DF4) = 256 slots of 0x20; the
# sprite-type table follows at 0x4DF4. (See pre2/checkpoints/blit.py.)
VAR_BLIT_TYPE = 0x4DF4
VAR_MASK_REGION = 0x2DF4
MASK_REGION_BYTES = VAR_BLIT_TYPE - VAR_MASK_REGION  # 0x2000


def read_blit_type_table(mem) -> bytes:
    """The 256-entry sprite-type table the blit dispatches on (1A13:0x4DF4)."""
    base = ((DATA_SEG << 4) + VAR_BLIT_TYPE) & 0xFFFFF
    return bytes(mem.data[base:base + 0x100])


def read_mask_region(mem) -> bytes:
    """The transparency-mask region (1A13:0x2DF4); mask for type t at (t-2)*0x20."""
    base = ((DATA_SEG << 4) + VAR_MASK_REGION) & 0xFFFFF
    return bytes(mem.data[base:base + MASK_REGION_BYTES])


def read_tilemap(mem, rows: int | None = None) -> TileMap:
    """Read the level tile map + the per-tile attribute tables.

    Tile indices come from the level segment [0x2DD6] (``rows*TILEMAP_STRIDE``
    bytes from offset 0; ``rows`` defaults to level height [0x2CF1]). The three
    attribute tables are read from the DATA segment 1A13 — 346E's xlatb carry an ES
    override (es=1A13), so the tables are there, not in the level block.
    """
    seg = _rw(mem, VAR_LEVEL_SEG)
    if rows is None:
        rows = _rb(mem, VAR_LEVEL_HEIGHT)
    flat = (seg << 4) & 0xFFFFF
    data = (DATA_SEG << 4) & 0xFFFFF

    def _tbl(off: int) -> bytes:
        return bytes(mem.data[data + off: data + off + 0x100])

    return TileMap(
        segment=seg,
        stride=TILEMAP_STRIDE,
        rows=rows,
        tiles=bytes(mem.data[flat: flat + rows * TILEMAP_STRIDE]),
        plane_attr=_tbl(TBL_PLANE_ATTR),
        tile_flags=_tbl(TBL_TILE_FLAGS),
        tile_type=_tbl(TBL_TILE_TYPE),
    )


def read_scroll_state(mem) -> ScrollState:
    return ScrollState(
        camera=read_camera(mem),
        scroll_src=_rw(mem, VAR_SCROLL_SRC),
        row_factor=_rb(mem, VAR_ROW_FACTOR),
        dest_page_a=_rw(mem, VAR_DEST_PAGE_A),
        dest_page_b=_rw(mem, VAR_DEST_PAGE_B),
        sheet_seg=_rw(mem, VAR_SHEET_SEG),
        level_height=_rb(mem, VAR_LEVEL_HEIGHT),
        dirty=_rb(mem, VAR_DIRTY),
        dirty_rows=_rb(mem, VAR_DIRTY_ROWS),
    )
