"""Source-cadence extraction of modern enhanced layers from the recovered state.

Run ONCE per ~25 fps source frame (NOT per display subframe). Uses the recovered/faithful planar code purely
as an EXTRACTOR/ORACLE — `render_frame` for the background-without-sprites and the full faithful frame, and
the verified `paint_sprite` to lift each sprite into a bg-independent RGBA texture. The output
(:class:`EnhancedFrameState`) is pure RGB/RGBA; the display compositor never touches planes.

Sprite RGBA extraction (the grounded trick): paint each sprite alone onto two CLEAN planar buffers — all-0x00
and all-0xFF — then de-index both. A pixel where the two AGREE is an opaque sprite pixel (its value is
bg-independent for NORMAL mask+sprite blits); where they DIFFER it left the background, i.e. transparent. So
agree -> opaque (colour = the value), differ -> alpha 0. OPAQUE/ERASE (flash/blink) sprites are bg-DEPENDENT
OR/mask blends, not standalone textures: they are NOT extracted (reported as unsupported), never faked.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from pre2.bridge.gameplay_effects import apply_gameplay_effects
from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.frame_state import EnhancedFrameState, SpriteInstance
from pre2.recovered.frame_renderer import build_background_ring, scroll_copy
from pre2.recovered.object_render import (LIST_TOP, MODE_NORMAL, RECORD_BYTES, paint_sprite,
                                          plan_sprite, plan_sprite_command)
from pre2.recovered.render_frame import ASSET_LO, _TileMapView, render_frame
from sdl_view import render_planar_rgb_from_planes

_ALL_RESTORE = bytes([1]) * 256   # force every tile -> type-1 restore_background (renders the backdrop only)

_OBJ_SEG = 0x1030 << 4   # active-list records live in segment 1030; the per-instance handle is byte 6

_MODE_NAME = {0x00: "ERASE", 0x01: "NORMAL", 0x10: "OPAQUE"}
# identity "palette": de-indexing with this returns the raw EGA index in the R channel (fast numpy path)
_ID_PAL = [(i, 0, 0) for i in range(256)]


def _indices(planes, page):
    """De-planarize a page to its EGA pixel indices (H×W uint8) using the fast RGB path + identity palette."""
    return render_planar_rgb_from_planes(planes, page, _ID_PAL)[:, :, 0]


def _extract_sprite_rgba(draw, src_bank, stride, page, palette):
    """Lift one NORMAL sprite into (rgba H×W×4, anchor_x, anchor_y) via the dual-buffer paint trick, or None
    if it left no opaque pixels (fully clipped)."""
    lo = [bytearray(0x10000) for _ in range(4)]
    hi = [bytearray(b"\xff" * 0x10000) for _ in range(4)]
    size = draw.src_bw * draw.full_rows * 6 + 64
    src = src_bank[draw.src_off:draw.src_off + size]
    paint_sprite(lo, draw, src, stride)
    paint_sprite(hi, draw, src, stride)
    idx_lo = _indices(lo, page)
    idx_hi = _indices(hi, page)
    agree = idx_lo == idx_hi                       # opaque sprite pixels (bg-independent value)
    ys, xs = np.nonzero(agree)
    if ys.size == 0:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub_idx = idx_lo[y0:y1, x0:x1]
    sub_mask = agree[y0:y1, x0:x1]
    pal = np.asarray(palette, dtype=np.uint8)
    rgba = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.uint8)
    rgba[..., :3] = pal[sub_idx]
    rgba[..., 3] = np.where(sub_mask, 255, 0).astype(np.uint8)
    return rgba, int(x0), int(y0)


def extract_enhanced_frame(mem, dos, *, game_root, with_faithful=True, effects=None) -> EnhancedFrameState | None:
    """Build the modern source-frame snapshot for a GAMEPLAY frame, or None if there is no object camera
    (i.e. not a gameplay frame -> the caller passes through faithful).

    ``with_faithful`` renders the full faithful frame into ``faithful_rgb`` (for parity/standalone use); the
    live viewer passes ``False`` since it already has the session's faithful frame (avoids a redundant render).
    ``effects`` (a GameplayEffects from the session: point particles / foreground / fireflies) is drawn into
    the background so the spider-web etc. appear and scroll with the camera (v1: in the bg layer, not yet
    velocity-interpolated; absent in the static-snapshot parity path which passes effects=None).
    """
    rs = read_renderer_state(mem, dos, game_root=game_root)
    cam = rs.object_camera
    if cam is None:
        return None
    page, stride = cam.dest_page, cam.row_stride
    palette = dos.vga_palette or [(0, 0, 0)] * 256

    bg_planes = [bytearray(0x10000) for _ in range(4)]
    render_frame(replace(rs, object_camera=None), bg_planes, palette, rebuild=True)
    if effects is not None:
        apply_gameplay_effects(bg_planes, page, effects)     # point particles / foreground / fireflies
    background_rgb = render_planar_rgb_from_planes(bg_planes, page, palette)

    # Backdrop-only render (the FIXED parallax base layer): same camera/scroll walk but every tile forced to
    # type-1 restore_background, so the visible page is purely the base layer showing through. The compositor
    # holds this still and scrolls only the tile layer (background_rgb != backdrop_rgb) -> no backdrop shake.
    backdrop_rgb = _render_backdrop(rs, page, palette)

    faithful_rgb = None
    if with_faithful:
        full_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(rs, full_planes, palette, rebuild=True)
        faithful_rgb = render_planar_rgb_from_planes(full_planes, page, palette)

    sprites, unsupported = [], []
    attrs = rs.object_attrs or {}
    banks = rs.object_src_banks or {}
    # camera in PIXELS, matching _placement: X = cam_x*16; Y = cam_y*16 + fine_scroll. Used to interpolate
    # the background scroll between source frames (objects stay glued to the scrolled bg).
    camera_px = (cam.cam_x * 16, cam.cam_y * 16 + cam.fine_scroll)
    # enumerate -> `slot` is the active-list record index (stable cross-frame identity, animation-independent)
    for slot, spr in enumerate(rs.object_sprites or ()):
        attr = attrs.get(spr.sprite_id)
        if attr is None:
            continue
        cmd = plan_sprite_command(spr, attr, cam)
        if cmd is None:
            continue
        if int(cmd.mode) != MODE_NORMAL:           # OPAQUE/ERASE: bg-dependent blend, not a texture
            unsupported.append((slot, cmd.base_id, _MODE_NAME.get(int(cmd.mode), hex(int(cmd.mode)))))
            continue
        draw = plan_sprite(spr, attr, cam)
        if draw is None:
            continue
        got = _extract_sprite_rgba(draw, banks.get(draw.src_seg, b""), stride, page, palette)
        if got is None:
            continue
        rgba, ax, ay = got
        rec = _OBJ_SEG + (LIST_TOP - slot * RECORD_BYTES)        # the object's persistent handle (pointer)
        handle = mem.data[rec + 6] | (mem.data[rec + 7] << 8)
        sprites.append(SpriteInstance(handle=handle, slot=slot, base_id=cmd.base_id, sprite_id=cmd.sprite_id,
                                      world_x=cmd.world_x, world_y=cmd.world_y,
                                      screen_x=cmd.screen_x, screen_y=cmd.screen_y,
                                      tex_off_x=ax - cmd.screen_x, tex_off_y=ay - cmd.screen_y,
                                      rgba=rgba, interpolate=not cmd.is_hud))
    return EnhancedFrameState(background_rgb=background_rgb, camera=camera_px,
                              sprites=sprites, faithful_rgb=faithful_rgb, unsupported=unsupported,
                              backdrop_rgb=backdrop_rgb)


def _render_backdrop(rs, page, palette):
    """Render the FIXED parallax base layer (sky/mountains) alone: replay the background-ring build + scroll
    copy with every tile forced to type-1 (``restore_background``), so the visible page is purely the base
    layer at 0x7E80. No animated/grid/sprite passes (the backdrop is the fixed layer those draw over)."""
    planes = [bytearray(0x10000) for _ in range(4)]
    if rs.asset_planes:                       # restore tile cache + parallax base into a clean framebuffer
        for p in range(4):
            planes[p][ASSET_LO:ASSET_LO + len(rs.asset_planes[p])] = rs.asset_planes[p]
    build_background_ring(planes, _TileMapView(rs), rs.camera_x, rs.camera_y, rs.scroll_src,
                          rs.col_ring, rs.fine_scroll, _ALL_RESTORE, rs.mask_region)
    scroll_copy(planes, rs.scroll_src, rs.dest_page, rs.col_ring, rs.fine_scroll,
                rs.row_ring, rs.row_factor)
    return render_planar_rgb_from_planes(planes, page, palette)
