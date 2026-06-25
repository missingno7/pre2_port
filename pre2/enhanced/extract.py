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
from pre2.recovered.object_render import (LIST_TOP, MODE_NORMAL, RECORD_BYTES, paint_sprite,
                                          plan_sprite, plan_sprite_command)
from pre2.recovered.render_frame import ASSET_LO, render_frame
from sdl_view import HEIGHT, WIDTH, _PLANAR_ROW_BYTES, render_planar_rgb_from_planes

_STRIDE = _PLANAR_ROW_BYTES       # 40 bytes/row (mode 0Dh planar), the page stride render_planar uses
# The parallax base layer is stored in screen layout at 0x7E80, so de-planarizing it directly reproduces the
# backdrop over the gameplay viewport: the ring-rebuild round-trip cancels fine_scroll (build_background_ring
# subtracts ROW_STRIDE*fine, scroll_copy adds SCREEN_ROW*fine back, both 0x28) -> net = the raw base. Verified
# viewport-exact across cameras / fine_scroll values. (Rows below the viewport are HUD and unused.)
_BACKDROP_BASE = 0x7E80
_BASE_OFF = _BACKDROP_BASE - ASSET_LO   # offset of the parallax base within RendererState.asset_planes
VIEWPORT_H = 176                         # gameplay viewport rows (the HUD strip below shows no backdrop)

_OBJ_SEG = 0x1030 << 4   # active-list records live in segment 1030; the per-instance handle is byte 6

_MODE_NAME = {0x00: "ERASE", 0x01: "NORMAL", 0x10: "OPAQUE"}
# identity "palette": de-indexing with this returns the raw EGA index in the R channel (fast numpy path)
_ID_PAL = [(i, 0, 0) for i in range(256)]


def _zero_base(asset_planes):
    """Return asset_planes with the parallax BASE layer (>= 0x7E80) zeroed but the tile-graphic cache
    (0x5E80..0x7E80) intact — so tiles still find their graphics but every base-showing pixel renders index 0."""
    return tuple(bytes(a[:_BASE_OFF]) + b"\x00" * (len(a) - _BASE_OFF) for a in asset_planes)


def _indices_window(planes, page, x0, y0, w, h):
    """De-planarize ONLY the screen window [x0:x0+w, y0:y0+h] to EGA indices (h×w uint8). Same math as
    render_planar (page stride 40, full-memory wrap), but over the sprite's tiny bbox instead of the whole
    320×200 page — the sprite extraction's dominant cost was two full-page deplanarizes per sprite."""
    bc0 = x0 >> 3                                    # first byte-column
    nbc = ((x0 + w + 7) >> 3) - bc0                  # byte-columns the window spans
    rowbase = (page + np.arange(y0, y0 + h) * _STRIDE + bc0) & 0xFFFF
    off = (rowbase[:, None] + np.arange(nbc)[None, :]) & 0xFFFF
    color = np.zeros((h, nbc, 8), dtype=np.uint8)
    for p in range(4):
        pb = np.frombuffer(planes[p], dtype=np.uint8)[off]   # bytearray view (no full-buffer copy); off gathers
        color |= np.unpackbits(pb[..., None], axis=2) << p   # only the window. MSB-first, exactly as render_planar
    sx = x0 - bc0 * 8                                # pixel x0 within the byte-aligned window
    return color.reshape(h, nbc * 8)[:, sx:sx + w]


def _extract_sprite_rgba(draw, cmd, src_bank, stride, page, palette):
    """Lift one NORMAL sprite into (rgba H×W×4, anchor_x, anchor_y) via the dual-buffer paint trick, or None
    if it left no opaque pixels (fully clipped). Only the sprite's on-screen bbox (from ``cmd``) is
    de-planarized, not the whole page."""
    x0 = max(0, cmd.screen_x)
    y0 = max(0, cmd.screen_y)
    w = min(WIDTH, cmd.screen_x + cmd.width) - x0
    h = min(HEIGHT, cmd.screen_y + cmd.height) - y0
    if w <= 0 or h <= 0:
        return None
    lo = [bytearray(0x10000) for _ in range(4)]
    hi = [bytearray(b"\xff" * 0x10000) for _ in range(4)]
    size = draw.src_bw * draw.full_rows * 6 + 64
    src = src_bank[draw.src_off:draw.src_off + size]
    paint_sprite(lo, draw, src, stride)
    paint_sprite(hi, draw, src, stride)
    idx_lo = _indices_window(lo, page, x0, y0, w, h)
    idx_hi = _indices_window(hi, page, x0, y0, w, h)
    agree = idx_lo == idx_hi                       # opaque sprite pixels (bg-independent value)
    ys, xs = np.nonzero(agree)
    if ys.size == 0:
        return None
    ay0, ay1, ax0, ax1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub_idx = idx_lo[ay0:ay1, ax0:ax1]
    sub_mask = agree[ay0:ay1, ax0:ax1]
    pal = np.asarray(palette, dtype=np.uint8)
    rgba = np.zeros((ay1 - ay0, ax1 - ax0, 4), dtype=np.uint8)
    rgba[..., :3] = pal[sub_idx]
    rgba[..., 3] = np.where(sub_mask, 255, 0).astype(np.uint8)
    return rgba, x0 + int(ax0), y0 + int(ay0)


def extract_enhanced_frame(mem, dos, *, game_root, with_faithful=True, effects=None) -> EnhancedFrameState | None:
    """Build the modern source-frame snapshot for a GAMEPLAY frame, or None if there is no object camera
    (i.e. not a gameplay frame -> the caller passes through faithful).

    ``with_faithful`` renders the full faithful frame into ``faithful_rgb`` (for parity/standalone use); the
    live viewer passes ``False`` since it already has the session's faithful frame (avoids a redundant render).
    ``effects`` (a GameplayEffects from the session: point particles / foreground tiles / fireflies) becomes a
    separate OVERLAY layer (overlay_rgb/overlay_mask) the compositor draws OVER the sprites — foreground tiles
    must be in front of sprites, and particles/fireflies draw on top. Absent in the parity path (effects=None).
    """
    rs = read_renderer_state(mem, dos, game_root=game_root)
    cam = rs.object_camera
    if cam is None:
        return None
    page, stride = cam.dest_page, cam.row_stride
    palette = dos.vga_palette or [(0, 0, 0)] * 256
    pal_rgb = np.asarray(palette, dtype=np.uint8)

    # Backdrop = the FIXED parallax base layer (sky/mountains), de-planarized directly from 0x7E80.
    backdrop_rgb = _render_backdrop(rs, page, palette)

    # Render the background over a ZEROED base instead of the real base: every base-showing pixel becomes
    # index 0, while opaque tile/effect pixels keep their (base-independent) colour. So tile_mask = index!=0
    # is the TRUE tile coverage (colour-independent), and the real background is reconstructed EXACTLY by
    # compositing those tile pixels over the backdrop (verified 0px). This costs the same one render as before
    # but yields the coverage the compositor needs to scroll the tile layer without leaving backdrop-coloured
    # tile pixels behind ("see-through" holes).
    bg0_planes = [bytearray(0x10000) for _ in range(4)]
    render_frame(replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes)),
                 bg0_planes, palette, rebuild=True)
    idx0 = render_planar_rgb_from_planes(bg0_planes, page, _ID_PAL)[:, :, 0]   # EGA indices over zeroed base
    tile_mask = idx0 != 0
    backdrop_full = backdrop_rgb.copy()
    backdrop_full[VIEWPORT_H:] = pal_rgb[0]                   # HUD rows: base-showing == palette[0] (panel bg)
    background_rgb = np.where(tile_mask[..., None], pal_rgb[idx0], backdrop_full)

    # Effect OVERLAY (foreground tiles + point particles + fireflies) — drawn over an EMPTY buffer. All three
    # are colour-0-keyed / OR-white, so index!=0 is exact coverage. Composited OVER the sprites by the
    # compositor (foreground tiles must be in FRONT of sprites). Skipped when no effects are active.
    overlay_rgb = overlay_mask = None
    if effects is not None:
        ov_planes = [bytearray(0x10000) for _ in range(4)]
        apply_gameplay_effects(ov_planes, page, effects)
        idx_ov = render_planar_rgb_from_planes(ov_planes, page, _ID_PAL)[:, :, 0]
        overlay_mask = idx_ov != 0
        overlay_rgb = pal_rgb[idx_ov]

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
        got = _extract_sprite_rgba(draw, cmd, banks.get(draw.src_seg, b""), stride, page, palette)
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
                              backdrop_rgb=backdrop_rgb, tile_mask=tile_mask,
                              overlay_rgb=overlay_rgb, overlay_mask=overlay_mask)


def _render_backdrop(rs, page, palette):
    """The FIXED parallax base layer (sky/mountains) over the gameplay viewport, by de-planarizing the base
    region (0x7E80) directly — see ``_BACKDROP_BASE``. ``page`` is unused (the base is screen-fixed)."""
    planes = [bytearray(0x10000) for _ in range(4)]
    if rs.asset_planes:                       # restore the parallax base into a clean framebuffer
        for p in range(4):
            planes[p][ASSET_LO:ASSET_LO + len(rs.asset_planes[p])] = rs.asset_planes[p]
    return render_planar_rgb_from_planes(planes, _BACKDROP_BASE, palette)
