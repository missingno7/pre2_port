"""Memory views for the frame-renderer / scroll-engine island.

VM memory ⇄ recovered dataclasses, the one place that knows *where* the camera
and scroll bookkeeping live in PRE2 memory (data segment ``1A0F``). Rendering
decisions belong in ``pre2/recovered/frame_renderer.py`` (the merge target); this
module only translates layout.

Field semantics are taken from disassembly (the "frame renderer / scroll engine"
section of ``docs/pre2/symbol_ledger.md``) and **confirmed by witness**: see
``pre2/probes/capture_frame_state.py`` + ``artifacts/frame_state_witness/``. The
witness showed, on demo 091827, ``row_ring_idx == camera_y % RING_ROWS`` exactly
as the camera panned 0→0x21, ``scroll_src`` tracking the camera off ``SCROLL_BASE``,
``[0x2DF5]`` counting tile-rows scrolled per frame, and ``[0x2DE0]`` carrying the
``0x55AA`` dirty sentinel ``3B5F`` writes.
"""
from __future__ import annotations

from dataclasses import dataclass

DATA_SEG = 0x1A0F        # GOG build

# --- visible-window / ring geometry (from the tile loops 348D/35A1 and 3344/338E) -
VISIBLE_COLS = 0x14      # 20 tiles across (cl=0x14)
VISIBLE_ROWS = 0x0C      # 12 tile rows drawn (ch=0x0C)
RING_COLS = 0x14         # column ring modulus (col index wraps 0..0x13)
RING_ROWS = 0x0C         # row ring modulus (row index wraps 0..0x0B; 12)
TILE_PX = 0x10           # fine pixel scroll wraps at one tile = 16 px
SCROLL_BASE = 0x3F40     # scroll-source ring-buffer base (value, not a ds offset)
DIRTY_SENTINEL = 0x55AA  # compositor seeds prev_x with this to force a full redraw

# --- data-segment variables (offsets within ds=1A0F; GOG = old + 4) ----------
VAR_CAMERA_X = 0x2DE4    # camera column, in tiles
VAR_CAMERA_Y = 0x2DE6    # camera row, in tiles
VAR_PREV_CAMERA_X = 0x2DE0  # previous camera X (dirty compare; also 0x55AA sentinel)
VAR_PREV_CAMERA_Y = 0x2DE2  # previous camera Y
VAR_COL_RING = 0x2DE8    # column ring index (camera_x % RING_COLS)
VAR_ROW_RING = 0x2DEA    # row ring index (camera_y % RING_ROWS)
VAR_FINE_SCROLL = 0x6BC4  # sub-tile pixel scroll (0..TILE_PX)
VAR_ROW_FACTOR = 0x6BF8  # row-stride factor (0x28 * this)
VAR_SCROLL_SRC = 0x2DBA  # scroll source offset into the ring buffer
VAR_DEST_PAGE_A = 0x2DD6  # double-buffer page offset (front/back; 0 or 0x2000)
VAR_DEST_PAGE_B = 0x2DD8  # the other double-buffer page offset
VAR_SHEET_SEG = 0x2DDA   # tilesheet segment used by the draw loops
VAR_LEVEL_HEIGHT = 0x2CF5  # level height in tile rows
VAR_BG_PTR = 0x2DF6      # background-restore source pointer (the blit's bg source base)
VAR_DIRTY = 0x2DF4       # composite dirty flags (rebuild-grid / type seen); also the
                         # tile-type accumulator the row draw ORs into
VAR_DIRTY_ROWS = 0x2DF5  # tile-rows scrolled this frame (reset after redraw)
# the two other per-row attribute accumulators the row draw ORs into:
VAR_PLANE_ATTR = 0x6BBD  # plane/attribute flags accumulator
VAR_TILE_FLAGS = 0x2DF2  # tile-flags accumulator

# --- tilemap layout (from 348D; witnessed) -----------------------------------
# The level segment [0x2DD6] holds the row-major tile map (1 byte/tile = tile
# index), the per-tile attribute tables, and (higher up) the sprite sheet. 348D
# reads the tile index with `lodsb` at offset (camera_y * TILEMAP_STRIDE +
# camera_x), confirmed by dump (row 33 = "21 44 6B 21 44 1D ... 7E 7E", 7E=sky).
VAR_LEVEL_SEG = 0x2DDA   # level-data block base segment (== sprites' VAR_LOCAL_BASE)
TILEMAP_STRIDE = 0x100   # bytes per tile row (caller passes ah=row,al=col -> si=row*256+col)
# per-tile attribute lookup tables, all in the level segment, indexed by tile index:
TBL_PLANE_ATTR = 0x6988  # xlatb -> OR into plane/attribute flags
TBL_TILE_FLAGS = 0x805E  # xlatb -> OR into tile flags
TBL_TILE_TYPE = 0x4DF8   # xlatb -> OR into type/dirty


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
    prev_x: int       # previous-frame camera X (dirty compare), [0x2DE0]
    prev_y: int       # previous-frame camera Y, [0x2DE2]
    col_ring: int     # column ring index, [0x2DE4]  (== x % RING_COLS)
    row_ring: int     # row ring index, [0x2DE6]      (== y % RING_ROWS)
    fine_scroll: int  # sub-tile pixel offset, [0x6BC4] (0..TILE_PX)

    @property
    def moved(self) -> bool:
        """Whether the camera differs from the previous frame (forces redraw).

        Matches 35A1's dirty test, including the ``0x55AA`` sentinel 3B5F writes
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
    dirty: int         # composite dirty flags, [0x2DF4]
    dirty_rows: int    # tile-rows scrolled this frame, [0x2DF5]


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
    # The three attribute tables live in the DATA segment 1A0F (348D's xlatb carry
    # an ES override, es=1A0F), NOT the level segment. tile_type IS the same table
    # the blit dispatches on (1A0F:0x4DF8).
    plane_attr: bytes      # 256-entry table: tile index -> plane/attr flags (1A0F:0x6988)
    tile_flags: bytes      # 256-entry table: tile index -> tile flags (1A0F:0x805E)
    tile_type: bytes       # 256-entry table: tile index -> type/dirty bits (1A0F:0x4DF8)

    def tile(self, col: int, row: int) -> int:
        """Tile index at (col, row); 348D reads this as ``lodsb`` at row*stride+col."""
        return self.tiles[(row * self.stride + col) % len(self.tiles)]

    def row_slice(self, col: int, row: int, count: int) -> bytes:
        """``count`` consecutive tile indices starting at (col, row) — one draw row."""
        start = row * self.stride + col
        return self.tiles[start:start + count]


def read_bg_off(mem) -> int:
    """The blit's background source offset: ``[0x2DF6] - 0x28 * [0x6BC4]``."""
    return (_rw(mem, VAR_BG_PTR) - 0x28 * _rb(mem, VAR_FINE_SCROLL)) & 0xFFFF


def write_bg_ptr(mem, value: int) -> None:
    """Persist the background-restore pointer ``[0x2DF6]`` (the value 348D/35A1 leave after a draw).

    This is LIVE state, not scratch: the per-sprite blit (1030:3B88) reads it back as
    ``bg_off = [0x2DF6] - 0x28 * [0x6BC4]`` (see :func:`read_bg_off`). The recovered tile/grid
    routines carry ``bg_ptr`` as a local, so the live hooks must write the final value here for any
    blit that runs before the next tile/grid call restarts it."""
    base = (DATA_SEG << 4) & 0xFFFFF
    v = value & 0xFFFF
    mem.data[base + VAR_BG_PTR] = v & 0xFF
    mem.data[base + VAR_BG_PTR + 1] = (v >> 8) & 0xFF


def read_row_flags(mem) -> tuple[int, int, int]:
    """The three per-row attribute accumulators 348D ORs into:
    ``(plane_attr [0x6BBD], tile_flags [0x2DF2], tile_type [0x2DF4])``."""
    return _rb(mem, VAR_PLANE_ATTR), _rb(mem, VAR_TILE_FLAGS), _rb(mem, VAR_DIRTY)


def write_row_flags(mem, plane_attr: int, tile_flags: int, tile_type: int) -> None:
    """Write the three row-flag accumulators back (the 348D write-back contract)."""
    base = (DATA_SEG << 4) & 0xFFFFF
    mem.data[base + VAR_PLANE_ATTR] = plane_attr & 0xFF
    mem.data[base + VAR_TILE_FLAGS] = tile_flags & 0xFF
    mem.data[base + VAR_DIRTY] = tile_type & 0xFF


def write_dirty_state(mem, prev_x: int, prev_y: int, *, dirty=None, dirty_rows=None,
                      tile_flags=None) -> None:
    """Write the grid-redraw side effects back (35A1): prev camera always; the dirty
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


# Blit input tables (1A0F domain), needed because the row draw composes the
# recovered blit. Masks occupy [0x2DF8 .. 0x4DF8) = 256 slots of 0x20; the
# sprite-type table follows at 0x4DF8. (See pre2/checkpoints/blit.py.)
VAR_BLIT_TYPE = 0x4DF8
VAR_MASK_REGION = 0x2DF8
MASK_REGION_BYTES = VAR_BLIT_TYPE - VAR_MASK_REGION  # 0x2000


def read_blit_type_table(mem) -> bytes:
    """The 256-entry sprite-type table the blit dispatches on (1A0F:0x4DF8)."""
    base = ((DATA_SEG << 4) + VAR_BLIT_TYPE) & 0xFFFFF
    return bytes(mem.data[base:base + 0x100])


def read_mask_region(mem) -> bytes:
    """The transparency-mask region (1A0F:0x2DF8); mask for type t at (t-2)*0x20."""
    base = ((DATA_SEG << 4) + VAR_MASK_REGION) & 0xFFFFF
    return bytes(mem.data[base:base + MASK_REGION_BYTES])


TILEMAP_WINDOW = 0x10000   # read the whole level-segment window for tile data


def read_tilemap(mem, rows: int | None = None) -> TileMap:
    """Read the level tile map + the per-tile attribute tables.

    Tile indices come from the level segment [0x2DD6]. We read the **full segment
    window** (not just ``level_height`` rows): the draws can address any offset the
    ASM does — when the camera reaches the level bottom the bottom-fill row is at
    row ``[0x2CF1]`` (one past the last 0-indexed row), and the ASM simply reads the
    segment memory there. Reading the window keeps ``tiles[si] == mem[level_seg:si]``
    for every ``si`` (so the recovered draw never goes out of bounds). ``rows`` is the
    level height (informational; the grid draws 12 visible rows). The three attribute
    tables are read from the DATA segment 1A0F (348D's xlatb carry an ES override).
    """
    seg = _rw(mem, VAR_LEVEL_SEG)
    if rows is None:
        rows = _rb(mem, VAR_LEVEL_HEIGHT)
    flat = (seg << 4) & 0xFFFFF
    data = (DATA_SEG << 4) & 0xFFFFF
    end = min(flat + TILEMAP_WINDOW, len(mem.data))

    def _tbl(off: int) -> bytes:
        return bytes(mem.data[data + off: data + off + 0x100])

    return TileMap(
        segment=seg,
        stride=TILEMAP_STRIDE,
        rows=rows,
        tiles=bytes(mem.data[flat:end]),
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
