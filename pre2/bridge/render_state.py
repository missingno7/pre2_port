"""Reconstruct the renderer's input contract (:class:`RendererState`) from VM memory.

Read-only, one place. This is the single bridge between original memory and the
consolidated, VM-independent :func:`pre2.recovered.render_frame.render_frame`. It reuses
the per-leaf readers in :mod:`pre2.bridge.frame` and :mod:`pre2.bridge.palette` so the
field semantics stay in one spot.
"""
from __future__ import annotations

from pre2.bridge import frame as _frame
from pre2.bridge import object_render as _obj
from pre2.bridge import palette as _pal
from pre2.recovered.render_frame import FadeStep, RendererState

_DS = 0x1A0F
_ANIM_FRAME_PTR = 0x6BC2   # [0x6BC2] = current animation-frame remap base


def _rb(mem, off):
    return mem.data[((_DS << 4) + off) & 0xFFFFF]


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _anim_xlat(mem) -> bytes:
    """The 256-byte remap slice for the current animation frame ([[0x6BC2] .. +256])."""
    base = ((_DS << 4) + _rw(mem, _ANIM_FRAME_PTR)) & 0xFFFFF
    return bytes(mem.data[base:base + 0x100])


def _fade_step(mem):
    """Resolve the palette-fade step (direction applied), or ``None`` if inactive."""
    if not _pal.fade_active(mem):
        return None
    fi = _pal.read_fade_inputs(mem)
    a, b = (fi.target, fi.src) if fi.direction != 0 else (fi.src, fi.target)
    return FadeStep(a=a, b=b, amount=fi.fade_amt)


def read_renderer_state(mem, *, frame_pre_inc: bool = True) -> RendererState:
    """Snapshot every renderer input from VM memory into a plain :class:`RendererState`.

    ``frame_pre_inc`` matches the object renderer's +1 to [0x6BD5] applied at 26FA entry
    (capture this state *before* that increment to see the value the engine will use)."""
    tm = _frame.read_tilemap(mem)
    st = _frame.read_scroll_state(mem)
    c = st.camera
    obj_cam, obj_sprites, obj_attrs, obj_banks = _obj.read_object_render_inputs(
        mem, frame_pre_inc=frame_pre_inc)
    return RendererState(
        tiles=tm.tiles,
        type_tbl=tm.tile_flags,        # 1A0F:0x805E
        flag_tbl=tm.plane_attr,        # 1A0F:0x6988
        blit_type=tm.tile_type,        # 1A0F:0x4DF8
        mask_region=_frame.read_mask_region(mem),
        anim_xlat=_anim_xlat(mem),
        camera_x=c.x,
        camera_y=c.y,
        prev_x=c.prev_x,
        prev_y=c.prev_y,
        col_ring=c.col_ring,
        fine_scroll=c.fine_scroll,
        row_ring=c.row_ring,
        scroll_src=st.scroll_src,
        dest_page=st.dest_page_b,
        row_factor=st.row_factor,
        dirty=st.dirty,
        dirty_rows=st.dirty_rows,
        fade=_fade_step(mem),
        object_camera=obj_cam,
        object_sprites=obj_sprites,
        object_attrs=obj_attrs,
        object_src_banks=obj_banks,
    )
