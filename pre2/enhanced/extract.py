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
from pre2.enhanced.sprite_cache import SpriteTexture, SpriteTextureCache, palette_version
from pre2.recovered.object_render import (LIST_TOP, MODE_NORMAL, RECORD_BYTES, paint_sprite,
                                          plan_sprite, plan_sprite_command)
from pre2.recovered.fireflies import _sar
from pre2.recovered.particles import advance_particle
from pre2.recovered.render_frame import ASSET_LO, render_frame
from sdl_view import _PLANAR_ROW_BYTES, render_planar_rgb_from_planes

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


def _extract_particles(pf):
    """Lift the one-shot point particles (4B8E) to interpolatable points: ``(screen_x, screen_y, vel_x,
    vel_y)`` for each on-screen particle, matching draw_particles' advance + cull + screen mapping exactly
    (so at alpha=1 the compositor plots the same pixel). vel is the particle's per-frame world delta (=
    screen delta), used to rewind it along its own path for sub-source-frame motion."""
    cam_x = (pf.cam_col << 4) & 0xFFFF
    cam_y = (pf.cam_row << 4) & 0xFFFF
    yb = (pf.y_bias & 0xFF) - 256 if pf.y_bias & 0x80 else pf.y_bias & 0xFF
    pts = []
    for (x, y, angle, speed) in pf.particles:
        nx, ny = advance_particle(x, y, angle, speed, pf.cos, pf.sin)
        sy = (ny - yb - cam_y) & 0xFFFF
        if sy >= 0xB0:                                  # off top/bottom (cull, as _plot_particle)
            continue
        sx = (nx - cam_x) & 0xFFFF
        if sx >= 0x140:                                 # off left/right
            continue
        vx = ((nx - x + 0x8000) & 0xFFFF) - 0x8000      # signed per-frame delta
        vy = ((ny - y + 0x8000) & 0xFFFF) - 0x8000
        pts.append((sx, sy, vx, vy))
    return pts


def _extract_fireflies(ff):
    """Lift the persistent firefly swarm (54AB) to interpolatable points: ``(slot, world_x, world_y,
    screen_x, screen_y)`` for each on-screen firefly, matching draw_fireflies' screen mapping exactly
    (so at alpha=1 the compositor plots the same pixel). ``slot`` is the persistent slot index used to
    match prev/cur and lerp the world position; ``world = (x>>3, y>>3)`` (the camera-relative draw uses
    those shifted coords)."""
    cam_x = (ff.cam_col << 4) & 0xFFFF
    cam_y = (ff.cam_row << 4) & 0xFFFF
    pts = []
    for idx, (x, y, _timer) in zip(ff.slot_idx or range(len(ff.slots)), ff.slots):
        wx, wy = _sar(x, 3), _sar(y, 3)
        sy = (wy - cam_y) & 0xFFFF
        if sy >= 0xB0:
            continue
        sx = (wx - cam_x) & 0xFFFF
        if sx >= 0x140:
            continue
        pts.append((idx, wx, wy, sx, sy))
    return pts


def _zero_base(asset_planes):
    """Return asset_planes with the parallax BASE layer (>= 0x7E80) zeroed but the tile-graphic cache
    (0x5E80..0x7E80) intact — so tiles still find their graphics but every base-showing pixel renders index 0."""
    return tuple(bytes(a[:_BASE_OFF]) + b"\x00" * (len(a) - _BASE_OFF) for a in asset_planes)


def _indices_window(planes, page, x0, y0, w, h, stride=_STRIDE):
    """De-planarize ONLY the screen window [x0:x0+w, y0:y0+h] to EGA indices (h×w uint8). Same math as
    render_planar (full-memory wrap), but over the sprite's tiny bbox instead of the whole 320×200 page — the
    sprite extraction's dominant cost was two full-page deplanarizes per sprite. ``stride`` is the row byte
    stride (40 for a real page; the canonical texture paint packs rows at ``src_bw``)."""
    bc0 = x0 >> 3                                    # first byte-column
    nbc = ((x0 + w + 7) >> 3) - bc0                  # byte-columns the window spans
    rowbase = (page + np.arange(y0, y0 + h) * stride + bc0) & 0xFFFF
    off = (rowbase[:, None] + np.arange(nbc)[None, :]) & 0xFFFF
    color = np.zeros((h, nbc, 8), dtype=np.uint8)
    for p in range(4):
        pb = np.frombuffer(planes[p], dtype=np.uint8)[off]   # bytearray view (no full-buffer copy); off gathers
        color |= np.unpackbits(pb[..., None], axis=2) << p   # only the window. MSB-first, exactly as render_planar
    sx = x0 - bc0 * 8                                # pixel x0 within the byte-aligned window
    return color.reshape(h, nbc * 8)[:, sx:sx + w]


def _texture_key(draw, attr):
    """The PALETTE- and POSITION-independent key for a sprite cel: only what changes its pixels -- cel
    identity (src segment + the cel's source offset), the full (unclipped) decoded geometry, flip, and draw
    mode. NOT screen/world position and NOT the off-screen clip (the cached texture is the full unclipped
    sprite; the compositor crops it). ``attr.src_off`` is the cel offset (``draw.src_off`` would fold in the
    top-clip skip -> position-dependent), ``draw.src_bw``/``full_rows`` are the full pre-clip dimensions."""
    return (draw.src_seg, attr.src_off, draw.src_bw, draw.full_rows, draw.flipped, draw.mode)


def _make_sprite_texture(draw, attr, src_bank):
    """Paint the FULL UNCLIPPED sprite cel via the dual-buffer trick and lift it to a palette-independent
    :class:`SpriteTexture` (the faithful cache-population path), or None if it has no opaque pixels.

    The sprite is painted at a CANONICAL position -- shift 0, rows packed at ``src_bw`` from offset 0 -- with
    NO clipping (full ``src_bw``×``full_rows``). De-planarizing gives ABSOLUTE pixel values, so canonical pixel
    ``k`` equals the on-screen pixel ``screen_x + k`` of the real (shifted, clipped) faithful paint -- i.e. the
    texture is identical to the faithful extraction for the visible part, and the compositor's edge-clipping
    ``_blit`` reproduces the clipped faithful exactly. ``off_x``/``off_y`` are the opaque bbox's top-left within
    the cel, so the compositor blits at ``screen_x + off_x``/``screen_y + off_y``."""
    src_bw, full_rows = draw.src_bw, draw.full_rows
    if src_bw <= 0 or full_rows <= 0:
        return None
    # An unclipped, canonical (shift 0, no top/left/right clip) copy of the draw -- identical pixels to a real
    # fully-on-screen draw (the case the previous cache already proved 0px), but reusable for edge sprites too.
    canon = replace(draw, dest_off=0, byte_width=src_bw, rows=full_rows, shift=0, clipped=False,
                    left_skip=0, right_skip=0, right_clipped=False, src_off=attr.src_off)
    lo = [bytearray(0x10000) for _ in range(4)]
    hi = [bytearray(b"\xff" * 0x10000) for _ in range(4)]
    size = src_bw * full_rows * 6 + 64
    src = src_bank[attr.src_off:attr.src_off + size]
    paint_sprite(lo, canon, src, src_bw)               # pack rows at src_bw (no overflow for any width)
    paint_sprite(hi, canon, src, src_bw)
    idx_lo = _indices_window(lo, 0, 0, 0, src_bw * 8, full_rows, stride=src_bw)
    idx_hi = _indices_window(hi, 0, 0, 0, src_bw * 8, full_rows, stride=src_bw)
    agree = idx_lo == idx_hi                            # opaque sprite pixels (bg-independent value)
    ys, xs = np.nonzero(agree)
    if ys.size == 0:
        return None
    ay0, ay1, ax0, ax1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return SpriteTexture(color_indices=idx_lo[ay0:ay1, ax0:ax1].copy(),
                         alpha_mask=agree[ay0:ay1, ax0:ax1].copy(),
                         off_x=int(ax0), off_y=int(ay0), mode=int(draw.mode))


def extract_enhanced_frame(mem, dos, *, game_root, with_faithful=True, effects=None,
                           tex_cache=None) -> EnhancedFrameState | None:
    """Build the modern source-frame snapshot for a GAMEPLAY frame, or None if there is no object camera
    (i.e. not a gameplay frame -> the caller passes through faithful).

    ``with_faithful`` renders the full faithful frame into ``faithful_rgb`` (for parity/standalone use); the
    live viewer passes ``False`` since it already has the session's faithful frame (avoids a redundant render).
    ``effects`` (a GameplayEffects from the session: point particles / foreground tiles / fireflies) becomes a
    separate OVERLAY layer (overlay_rgb/overlay_mask) the compositor draws OVER the sprites — foreground tiles
    must be in front of sprites, and particles/fireflies draw on top. Absent in the parity path (effects=None).
    ``tex_cache`` (a :class:`~pre2.enhanced.sprite_cache.SpriteTextureCache`) persists cel textures across
    source frames; a throwaway one is made when None (parity path) -> identical output, no cross-frame reuse.
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

    # Effect OVERLAY (foreground tiles + fireflies) — drawn over an EMPTY buffer (both colour-0-keyed /
    # OR-white, so index!=0 is exact coverage). Composited OVER the sprites. One-shot point particles are
    # pulled OUT to a point list (below) so they can be velocity-interpolated; engine order is particles ->
    # foreground -> fireflies, so the compositor draws the particle points UNDER this overlay.
    overlay_rgb = overlay_mask = particle_rgb = firefly_rgb = None
    particles = []
    fireflies = []
    if effects is not None:
        # Overlay = FOREGROUND TILES only. Particles + fireflies are pulled out to point lists so they can be
        # interpolated (particles by velocity, fireflies by slot); the compositor draws them in engine order
        # (particles UNDER the foreground overlay, fireflies OVER it).
        ov_planes = [bytearray(0x10000) for _ in range(4)]
        ov_fx = replace(effects, particles=None, fireflies=None)
        if ov_fx.foreground is not None and ov_fx.foreground.page != page:
            # The foreground state is snapshotted at the 3732 hook, whose page is the back page BEFORE the
            # per-frame flip; render it into the SAME page we de-planarize at (cam.dest_page) -- the camera is
            # unchanged within the frame, so only the page base differs.
            ov_fx = replace(ov_fx, foreground=replace(ov_fx.foreground, page=page))
        apply_gameplay_effects(ov_planes, page, ov_fx)
        idx_ov = render_planar_rgb_from_planes(ov_planes, page, _ID_PAL)[:, :, 0]
        overlay_mask = idx_ov != 0
        overlay_rgb = pal_rgb[idx_ov]
        if effects.particles is not None:
            particles = _extract_particles(effects.particles)
            particle_rgb = tuple(int(c) for c in pal_rgb[15])    # 4B8E plots colour 15 (white)
        if effects.fireflies is not None:
            fireflies = _extract_fireflies(effects.fireflies)
            firefly_rgb = tuple(int(c) for c in pal_rgb[15])     # VM oracle collapses the 14/15 flicker to 15

    faithful_rgb = None
    if with_faithful:
        full_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(rs, full_planes, palette, rebuild=True)
        faithful_rgb = render_planar_rgb_from_planes(full_planes, page, palette)

    sprites, unsupported = [], []
    attrs = rs.object_attrs or {}
    banks = rs.object_src_banks or {}
    # Sprite texture cache (layer A): the palette-INDEPENDENT cel textures are reused across source frames when
    # the session passes a persistent ``tex_cache`` (steady gameplay re-extracts only cels that actually
    # changed), else a throwaway cache (the parity path -> identical output, just no cross-frame reuse). The
    # palette is applied per frame, so fades never invalidate the cache.
    cache = tex_cache if tex_cache is not None else SpriteTextureCache()
    pversion = palette_version(palette)
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
        key = _texture_key(draw, attr)
        tex = cache.get(key)
        if tex is None:                            # miss -> faithful paint/deplanarize POPULATES the cache
            tex = _make_sprite_texture(draw, attr, banks.get(draw.src_seg, b""))
            if tex is None:
                continue                           # no opaque pixels (don't cache empties)
            cache.put(key, tex)
        rgba = cache.colorize(key, tex, palette, pversion)   # apply the current palette (memoised per version)
        rec = _OBJ_SEG + (LIST_TOP - slot * RECORD_BYTES)        # the object's persistent handle (pointer)
        handle = mem.data[rec + 6] | (mem.data[rec + 7] << 8)
        sprites.append(SpriteInstance(handle=handle, slot=slot, base_id=cmd.base_id, sprite_id=cmd.sprite_id,
                                      world_x=cmd.world_x, world_y=cmd.world_y,
                                      screen_x=cmd.screen_x, screen_y=cmd.screen_y,
                                      tex_off_x=tex.off_x, tex_off_y=tex.off_y,
                                      rgba=rgba, interpolate=not cmd.is_hud))
    return EnhancedFrameState(background_rgb=background_rgb, camera=camera_px,
                              sprites=sprites, faithful_rgb=faithful_rgb, unsupported=unsupported,
                              backdrop_rgb=backdrop_rgb, tile_mask=tile_mask,
                              overlay_rgb=overlay_rgb, overlay_mask=overlay_mask,
                              particles=particles, particle_rgb=particle_rgb,
                              fireflies=fireflies, firefly_rgb=firefly_rgb,
                              iris=rs.iris, page=page)


def _render_backdrop(rs, page, palette):
    """The FIXED parallax base layer (sky/mountains) over the gameplay viewport, by de-planarizing the base
    region (0x7E80) directly — see ``_BACKDROP_BASE``. ``page`` is unused (the base is screen-fixed)."""
    planes = [bytearray(0x10000) for _ in range(4)]
    if rs.asset_planes:                       # restore the parallax base into a clean framebuffer
        for p in range(4):
            planes[p][ASSET_LO:ASSET_LO + len(rs.asset_planes[p])] = rs.asset_planes[p]
    return render_planar_rgb_from_planes(planes, _BACKDROP_BASE, palette)
