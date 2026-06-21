"""Memory views for the moving-sprite renderer island (VM memory ⇄ dataclasses).

The one place that knows *where* the active-sprite list, the per-sprite attribute
tables, and the camera/scroll inputs live in PRE2 memory. Rendering decisions live
in ``pre2/recovered/object_render.py``; this module only translates layout.

Layout (data segment ``1A0F``; see docs/pre2/symbol_ledger.md "1030:26FA"):
active list ``[0x4F0A..0x5720]`` 18-byte records (top->down draw order); attribute
tables indexed by ``id<<1`` at width/height ``0x7190``, x/y offset ``0x752A``,
sprite-data segment ``0x62E8`` and offset ``0x5F48``.
"""
from __future__ import annotations

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

from pre2.recovered.object_render import (
    LIST_BASE, LIST_TOP, RECORD_BYTES, Camera, Sprite, SpriteAttr,
)

DATA_SEG = 0x1A0F
CODE_SEG = 0x1030
PLANE_BYTES = EGA_PLANE_STRIDE   # 0x10000 per EGA plane

# data-segment variables
VAR_CAMERA_X = 0x2DE4
VAR_CAMERA_Y = 0x2DE6
VAR_FINE_SCROLL = 0x6BC4
VAR_ROW_FACTOR = 0x6BF8
VAR_DEST_PAGE = 0x2DD8
VAR_ROW_STRIDE = 0x2DB0
VAR_CURSOR = 0x2DEE          # [0x2DEE] the active-list cursor (record ptr)
VAR_FRAME = 0x6BD5           # [0x6BD5] frame counter (incremented at 26FA entry)

# per-sprite attribute tables (indexed by id<<1)
TBL_WIDTH_HEIGHT = 0x7190    # word: low=width(src bytes), high=height(rows)
TBL_XY_OFFSET = 0x752A       # byte pair: [+0]=x_off, [+1]=y_off
TBL_SRC_SEG = 0x62E8         # word: sprite pixel-data segment
TBL_SRC_OFF = 0x5F48         # word: sprite pixel-data offset

# cs:[0] global pixel-shift divisor
VAR_GLOBAL_SHIFT = 0x0000


def _rb(mem, seg, off):
    return mem.data[((seg << 4) + off) & 0xFFFFF]


def _rw(mem, seg, off):
    b = ((seg << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def read_camera(mem, *, frame_pre_inc: bool = True) -> Camera:
    """``frame_pre_inc`` adds the +1 the engine applies to [0x6BD5] at 26FA entry,
    so callers hooking *before* that increment see the value the engine will use."""
    frame = _rw(mem, DATA_SEG, VAR_FRAME)
    if frame_pre_inc:
        frame = (frame + 1) & 0xFFFF
    return Camera(
        cam_x=_rw(mem, DATA_SEG, VAR_CAMERA_X),
        cam_y=_rw(mem, DATA_SEG, VAR_CAMERA_Y),
        fine_scroll=_rb(mem, DATA_SEG, VAR_FINE_SCROLL),
        row_factor=_rw(mem, DATA_SEG, VAR_ROW_FACTOR),
        dest_page=_rw(mem, DATA_SEG, VAR_DEST_PAGE),
        row_stride=_rw(mem, DATA_SEG, VAR_ROW_STRIDE),
        global_shift=_rb(mem, CODE_SEG, VAR_GLOBAL_SHIFT),
        frame=frame,
    )


def read_planes(mem) -> list[bytearray]:
    """The four EGA shadow planes (64 KiB each) as parallel byte buffers."""
    return [bytearray(mem.data[EGA_APERTURE + p * PLANE_BYTES:
                               EGA_APERTURE + (p + 1) * PLANE_BYTES]) for p in range(4)]


def read_source(mem, seg: int, off: int, length: int) -> bytes:
    """Sprite pixel bytes from ``seg:off`` (the blit's source pointer)."""
    base = ((seg << 4) + off) & 0xFFFFF
    return bytes(mem.data[base:base + length])


def read_sprite(mem, off: int) -> Sprite:
    return Sprite(
        x=_rw(mem, DATA_SEG, off + 0),
        y=_rw(mem, DATA_SEG, off + 2),
        sprite_id=_rw(mem, DATA_SEG, off + 4),
        flags=_rb(mem, DATA_SEG, off + 5),
        life=_rb(mem, DATA_SEG, off + 0x11),
    )


def read_attr(mem, sprite_id: int) -> SpriteAttr:
    # The id word [si+4] carries flags in its high byte: bit 0x2000 = "drawn"
    # (set at 28B6, cleared at 2732 each frame) and bit 0x8000 = H-flip. The table
    # index is the id with those cleared, <<1 [asm 2732 and 0xDF / 2739 shl / 275A and bh,0x7F].
    bx = ((sprite_id & 0x5FFF) << 1) & 0xFFFF
    wh = _rw(mem, DATA_SEG, TBL_WIDTH_HEIGHT + bx)
    return SpriteAttr(
        width=wh & 0xFF,
        height=(wh >> 8) & 0xFF,
        x_off=_rb(mem, DATA_SEG, TBL_XY_OFFSET + bx),
        y_off=_rb(mem, DATA_SEG, TBL_XY_OFFSET + bx + 1),
        src_seg=_rw(mem, DATA_SEG, TBL_SRC_SEG + bx),
        src_off=_rw(mem, DATA_SEG, TBL_SRC_OFF + bx),
    )


def read_active_list(mem):
    """Records in the ASM's processing order: cursor top (0x5720) down to base."""
    out = []
    off = LIST_TOP
    while off >= LIST_BASE:
        out.append((off, read_sprite(mem, off)))
        off -= RECORD_BYTES
    return out
