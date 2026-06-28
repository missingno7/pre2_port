"""VM seam for the projectile/player interaction pass (1030:8C21 / 899E).

Layout + render-integration only — no gameplay decisions (those are in
:mod:`pre2.recovered.combat_interaction`). This layer translates the recovered write contracts onto the live
VM and performs the one render side-effect of the pass: the on-screen tile re-blit a bonus collect triggers.
"""
from __future__ import annotations

from pre2.bridge import frame as _frame
from pre2.bridge import sprites as _spr
from pre2.recovered.renderer import blit_sprite

DATA_SEG = 0x1A0F
MAP_SEG_PTR = 0x2DDA       # [0x2DDA] = the level-map segment (the es for the map writes)
GRID_ROWS = 0x0C           # the visible grid is 12 tile-rows
GRID_COLS = 0x14           # ... x 20 tile-cols
ROW_STRIDE = 0x280         # bytes per visible tile-row on the page


def readers(mem):
    """``(rb, rw)`` byte/word readers over DGROUP (1A0F)."""
    base = (DATA_SEG << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        b = (base + (o & 0xFFFF)) & 0xFFFFF
        return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)

    return rb, rw


def apply_ds(mem, writes) -> None:
    """Apply a recovered byte-level ``{offset: value}`` DGROUP contract."""
    base = (DATA_SEG << 4) & 0xFFFFF
    for off, val in writes.items():
        mem.data[(base + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF


def apply_map(mem, map_writes) -> None:
    """Apply the recovered ``{offset: (value, width)}`` writes into the level-map segment (es=[0x2DDA])."""
    es = mem.rw(DATA_SEG, MAP_SEG_PTR)
    base = (es << 4) & 0xFFFFF
    for off, (val, width) in map_writes.items():
        mem.data[(base + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF
        if width == 2:
            mem.data[(base + ((off + 1) & 0xFFFF)) & 0xFFFFF] = (val >> 8) & 0xFF


def screen_di(map_offset: int) -> int:
    """[asm 8BA6-8BD2] On-screen tile offset for a level-map cell offset (row<<8 | col):
    ``0x280*(row % 12) + 2*(col % 20)`` on the scroll-ring page."""
    row = (map_offset >> 8) & 0xFF
    col = map_offset & 0xFF
    return (ROW_STRIDE * (row % GRID_ROWS) + 2 * (col % GRID_COLS)) & 0xFFFF


def redraw_tiles(mem, redraws, map_writes) -> None:
    """[asm 8B77's redraw] Re-blit each on-screen collected tile. (453B just sets EGA write-mode-1/mask-0x0F,
    which the recovered blit_sprite doesn't need; 3B77 is the recovered blit.) The tile id is the value that
    was restored into the level map at that cell offset.

    ``screen_di`` is the page-RELATIVE offset (the ASM 8BA6-8BD2 math); the shared blit entry 3B77 then adds
    the off-screen scroll-staging base ``0x3F40`` (``add di,0x3f40``) before copying into A000 — every recovered
    blit caller composes its di in that same staging frame, so the tile redraw must add it too."""
    if not redraws:
        return
    planes = _spr.plane_views(mem)
    blit_type = _frame.read_blit_type_table(mem)
    mask_region = _frame.read_mask_region(mem)
    bg_off = _frame.read_bg_off(mem)
    for map_offset in redraws:
        tile_id = map_writes[map_offset][0]
        typ = blit_type[tile_id]
        mask = mask_region[(typ - 2) * 0x20:(typ - 2) * 0x20 + 0x20] if typ >= 2 else b""
        di = (screen_di(map_offset) + _frame.SCROLL_BASE) & 0xFFFF     # [asm 3B77: add di,0x3f40]
        blit_sprite(planes, tile_id, di, typ, bg_off, mask)
