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
from time import perf_counter as _perf

from pre2.enhanced.frame_state import EnhancedFrameState, SpriteInstance
from pre2.enhanced.native_background import (NativeBackgroundUnsupported, TileTextureCache, _HudCache,
                                             native_background_indices)
from pre2.enhanced.sprite_cache import SpriteTexture, SpriteTextureCache, palette_version
from pre2.recovered.object_render import (LIST_TOP, MODE_ERASE, MODE_NORMAL, MODE_OPAQUE, RECORD_BYTES, _s16,
                                          paint_sprite, plan_sprite, plan_sprite_command)
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

# --- sprite IDENTITY + true-motion sources (the interpolation anchors) --------------------------------------
# The render-slot array lives in DGROUP (0x4F0A..0x5720, stride 0x12; read_active_list iterates TOP-DOWN, so
# enumerate index i -> DS record LIST_TOP - i*0x12). A PROJECTED entity record's [+6] is the back-pointer
# project_entity (7F26) copies from the owning 2nd-pass entry — the per-instance identity, and that entry
# holds the TRUE world position ([entry+9]=X, [entry+0xB]=Y), free of per-cel anim anchors. The two FIXED
# records repurpose [+6] (the player's is its Xvel!), so they get fixed identities instead — and the CLUB
# overlay (0x4F0A, the top-most attack sprite) is rigidly attached to the PLAYER, so its motion source is the
# player's world position: its own slot x/y swings by cel anchor per attack pose (31->35->65 px in one swing),
# which is POSE, not motion — lerping it was the "player shakes while bashing" bug. (The old code also read
# the handle bytes from the CODE segment at the DS offset — constant code bytes per slot, colliding at 0 for
# player+club, which lerped the player against the club's previous position.)
_DS_BASE = 0x1A0F << 4
_PLAYER_REC = 0x4F1C
_CLUB_REC = 0x4F0A
_ENTITY_LO, _ENTITY_HI = 0x8489, 0x9107   # the 2nd-pass entity + float-effect records (owner-ptr range)
_FX_REC_LO, _FX_REC_HI = 0x52E8, 0x5450   # the 8922 effect draw-list slots (their [+9] = the source entry)
_FX_SRC_LO = 0x8F1D                       # the float-effect source list (stride 7)

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


def _replan_wide(spr, attr, cam, margin):
    """TRUE-WIDESCREEN re-admission (Experimental): the faithful planner culls a record whose sprite lies
    outside the original 320px window; when the record exists in the render list but only fails the X-window
    cull, replan it as if it were on-screen and give back its real placement. READ-ONLY — the game's state
    (including the render list) is never touched; this only widens the presentation cull, so record/state
    digests stay byte-exact. The trick: shift the camera by WHOLE TILES (16px, preserving ``screen_x & 7``)
    to bring the sprite into the faithful window, plan there (mode/blink/flip/Y-cull all evaluated by the
    UNMODIFIED recovered planner), then shift the resulting placement back. Returns ``(cmd, k_tiles)`` or
    None (record empty / outside the wide window too / Y-culled)."""
    if spr.sprite_id == 0xFFFF:
        return None
    x_off = attr.width - attr.x_off if (spr.sprite_id & 0x8000) else attr.x_off
    sx = _s16(spr.x - x_off - cam.cam_x * 16)               # the pre-cull screen X [asm 277A..27B5]
    if not (-(attr.width + margin) < sx < 320 + margin):    # outside even the WIDE window
        return None
    k = (sx - 152) // 16                                    # tile shift landing sx' in [152,167] (mid-window)
    cmd = plan_sprite_command(spr, attr, replace(cam, cam_x=cam.cam_x + k))
    if cmd is None:                                          # Y-baseline culled (or empty) even mid-window
        return None
    return replace(cmd, screen_x=cmd.screen_x + 16 * k), k


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
                           tex_cache=None, bg_cache=None, margin=0,
                           wide_cull=False) -> EnhancedFrameState | None:
    """Build the modern source-frame snapshot for a GAMEPLAY frame, or None if there is no object camera
    (i.e. not a gameplay frame -> the caller passes through faithful).

    ``with_faithful`` renders the full faithful frame into ``faithful_rgb`` (for parity/standalone use); the
    live viewer passes ``False`` since it already has the session's faithful frame (avoids a redundant render).
    ``effects`` (a GameplayEffects from the session: point particles / foreground tiles / fireflies) becomes a
    separate OVERLAY layer (overlay_rgb/overlay_mask) the compositor draws OVER the sprites — foreground tiles
    must be in front of sprites, and particles/fireflies draw on top. Absent in the parity path (effects=None).
    ``tex_cache`` (a :class:`~pre2.enhanced.sprite_cache.SpriteTextureCache`) persists cel textures across
    source frames; a throwaway one is made when None (parity path) -> identical output, no cross-frame reuse.
    ``bg_cache`` is a ``(TileTextureCache, _HudCache)`` pair persisting the native-background tile/HUD textures
    likewise (a throwaway pair when None).
    ``margin`` (px each side) is WIDESCREEN: the tile background renders wider straight from the recovered
    tilemap (real level content); every screen-space X (sprites, particles, fireflies, overlay placement) is
    shifted by +margin; the backdrop and HUD strip (no wider source exists) extend by edge-pixel replication.
    The central 320 columns are IDENTICAL to margin=0 (widescreen only ADDS pixels outside the faithful
    window — except the backdrop, which is fit-to-width scaled); entities fully outside the original window
    pop in at its edge (the game never emits render records for them) — the standard widescreen-mod artifact.
    margin=0 (the default and the parity path) is byte-identical to the pre-widescreen extractor.
    ``wide_cull`` (TRUE WIDESCREEN, Experimental) re-admits render records the faithful planner culls at the
    320px boundary, so entities in the margins draw instead of popping in/out at the faithful edge. Read-only
    (state digests stay byte-exact) but the PRESENTATION deliberately shows what the original would not.
    Only records the game's own producers emitted are covered — entities beyond the producers' window still
    pop in at the wide edge.
    """
    rs = read_renderer_state(mem, dos, game_root=game_root)
    cam = rs.object_camera
    if cam is None:
        return None
    page, stride = cam.dest_page, cam.row_stride
    palette = dos.vga_palette or [(0, 0, 0)] * 256
    pal_rgb = np.asarray(palette, dtype=np.uint8)

    # Backdrop = the FIXED parallax base layer (sky/mountains), de-planarized directly from 0x7E80.
    # Widescreen: the backdrop is a fixed 320px image with no wider source -> FIT TO WIDTH: scale the viewport
    # portion up to the wide width (nearest-neighbour, preserving aspect) and centre-crop the height overflow.
    backdrop_rgb = _render_backdrop(rs, page, palette)
    if margin:
        backdrop_rgb = _fit_backdrop_width(backdrop_rgb, 320 + 2 * margin)

    # Background colour INDICES over a zeroed base: every base-showing pixel is index 0, opaque tile/effect
    # pixels keep their (base-independent) colour. So tile_mask = index!=0 is the TRUE tile coverage
    # (colour-independent) and the real background is reconstructed EXACTLY by compositing those tile pixels
    # over the backdrop (verified 0px) -- the coverage the compositor needs to scroll the tile layer without
    # leaving backdrop-coloured tile pixels behind ("see-through" holes).
    #
    # LAYER B: build idx0 NATIVELY from the recovered tilemap + cached tile textures (no render_frame ring
    # rebuild, ~6ms -> ~0.15ms). The native path is byte-identical to the faithful render (proven 0px,
    # pre2/probes/verify_native_background.py); anything it doesn't cover raises NativeBackgroundUnsupported and
    # we fall back to the faithful render EXPLICITLY (counted in stats.fallbacks -- never a silent approximation).
    if bg_cache is None:
        bg_cache = (TileTextureCache(), _HudCache())
    tile_cache, hud_cache = bg_cache
    _t0 = _perf()
    try:
        idx0 = native_background_indices(rs, tile_cache, hud_cache, margin=margin)
    except NativeBackgroundUnsupported:
        tile_cache.stats["fallbacks"] += 1
        bg0_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes)),
                     bg0_planes, palette, rebuild=True)
        idx0 = render_planar_rgb_from_planes(bg0_planes, page, _ID_PAL)[:, :, 0]
        if margin:                                       # faithful fallback is 320-wide -> edge-extend
            idx0 = np.pad(idx0, ((0, 0), (margin, margin)), mode="edge")
    tile_cache.stats["native_s"] += _perf() - _t0
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
        if margin:                                       # screen-space records: place at +margin, empty margins
            idx_ov = np.pad(idx_ov, ((0, 0), (margin, margin)))
        overlay_mask = idx_ov != 0
        overlay_rgb = pal_rgb[idx_ov]
        if effects.particles is not None:
            particles = [(sx + margin, sy, vx, vy)
                         for (sx, sy, vx, vy) in _extract_particles(effects.particles)]
            particle_rgb = tuple(int(c) for c in pal_rgb[15])    # 4B8E plots colour 15 (white)
        if effects.fireflies is not None:
            fireflies = [(i, wx, wy, sx + margin, sy)
                         for (i, wx, wy, sx, sy) in _extract_fireflies(effects.fireflies)]
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
    # camera in PIXELS, matching the sprite placement: X = cam_x*16; Y = cam_y*16 + fine_scroll - row_factor
    # (screen_y = world_y + y_off + row_factor - (cam_y*16 + fine) — so the SHAKE bias [0x6BF8] is part of
    # the effective camera; including it makes the camera delta carry the shake and the WHOLE frame shakes
    # between subframes, not just the sprites). Used to interpolate the background scroll between ticks.
    camera_px = (cam.cam_x * 16, cam.cam_y * 16 + cam.fine_scroll - cam.row_factor)
    # enumerate -> `slot` is the active-list record index (stable cross-frame identity, animation-independent)
    for slot, spr in enumerate(rs.object_sprites or ()):
        attr = attrs.get(spr.sprite_id)
        if attr is None:
            continue
        cmd = plan_sprite_command(spr, attr, cam)
        kshift = 0
        if cmd is None:
            if wide_cull and margin:                        # TRUE WIDESCREEN: re-admit an X-culled record
                res = _replan_wide(spr, attr, cam, margin)
                if res is None:
                    continue
                cmd, kshift = res
            else:
                continue
        mode = int(cmd.mode)
        if mode not in (MODE_NORMAL, MODE_OPAQUE, MODE_ERASE):
            unsupported.append((slot, cmd.base_id, _MODE_NAME.get(mode, hex(mode))))
            continue
        # The three real blit modes, all reproduced by the SAME dual-buffer bake (paint_sprite over an all-0 and
        # an all-1 background; pixels that agree are background-independent and texturable):
        #   NORMAL — the sprite's own colours.
        #   OPAQUE (id bit14, the one-frame hit/death flash) — paint_sprite ORs the sprite's mask into all four
        #     planes, so covered pixels are index 15 (white) regardless of background.
        #   ERASE (blink-off, 3/4 frames of an invuln/pickup blink) — the SAME silhouette as the flash but ANDed:
        #     covered pixels become index 0 (black). "Hit-flash but black."
        # CAVEAT (accepted): unlike the white flash, the black silhouette exposes a sub-byte-shift imprecision in
        # the position-INDEPENDENT (canonical shift-0) bake — it is a strict SUPERSET of the true erase footprint
        # (0px over-draw at shift 0, growing to a ~9px black fringe at shift 7 = screen_x & 7). Pure over-draw, so
        # no holes; the blink reads as a slightly-oversized black silhouette. An exact fix needs a per-shift /
        # background-dependent erase paint (also handling the player+club dual blink + edge clip); deferred.
        # For a re-admitted (true-widescreen) record, plan the raster geometry at the SHIFTED camera (the
        # sprite is mid-window there -> unclipped); the texture key/bake are position-independent.
        draw = plan_sprite(spr, attr, cam if kshift == 0 else replace(cam, cam_x=cam.cam_x + kshift))
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
        rec_off = LIST_TOP - slot * RECORD_BYTES                 # this record's DS offset (top-down list)
        # MOTION always = the record's own [+0]/[+2] (cmd.world_x/world_y) — the quantity screen_x/screen_y
        # are derived from, so the interpolation rewind is consistent with the drawn position for EVERY class.
        # (The earlier attempt to read motion from the owning ENTRY was a scale/space mismatch: entry[+9] and
        # the projected record[+0] differ, so a moving enemy blitted at screen_x while rewinding by the entry
        # delta = broken/absent interpolation — the "monster not interpolated" report.) IDENTITY is separate:
        # take it from the owning object so it survives active-list compaction.
        wx, wy = cmd.world_x, cmd.world_y
        if rec_off == _PLAYER_REC:
            handle = ("player",)                                 # fixed record: [+6] is the Xvel, not a pointer
        elif rec_off == _CLUB_REC:
            handle = ("club",)                                   # the attack overlay's OWN slot swings by attack
            d = mem.data                                         # pose (31->35->65px) — that is pose, not motion,
            wx = d[_DS_BASE + _PLAYER_REC] | (d[_DS_BASE + _PLAYER_REC + 1] << 8)  # so its motion is the player's
            wy = d[_DS_BASE + _PLAYER_REC + 2] | (d[_DS_BASE + _PLAYER_REC + 3] << 8)
        else:
            d = mem.data
            owner = d[_DS_BASE + rec_off + 6] | (d[_DS_BASE + rec_off + 7] << 8)   # [+6] back-pointer (7F26)
            src9 = d[_DS_BASE + rec_off + 9] | (d[_DS_BASE + rec_off + 10] << 8)   # [+9] source back-ref (8922)
            if _ENTITY_LO <= owner < _ENTITY_HI:                 # a projected 2nd-pass entity (enemies etc.)
                handle = ("ent", owner)                          #   identity = the owning entry (stable)
            elif _FX_REC_LO <= rec_off < _FX_REC_HI and _FX_SRC_LO <= src9 < _ENTITY_HI:
                handle = ("fx", src9)                            # 8922-projected effect (bonus items/popups):
                #  identity = the float-effect SOURCE entry — the effect sub-list COMPACTS every tick as items
                #  scroll in/out, so record-address identity slid one item's motion onto its neighbour (the
                #  classic slot-jitter); the source ref is instance-stable across compaction.
            else:                                                # producer/object record: slot-stable identity
                handle = ("rec", rec_off)
        sprites.append(SpriteInstance(handle=handle, slot=slot, base_id=cmd.base_id, sprite_id=cmd.sprite_id,
                                      world_x=wx, world_y=wy,
                                      screen_x=cmd.screen_x + margin, screen_y=cmd.screen_y,
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


def _fit_backdrop_width(backdrop, wide_w: int):
    """Widescreen backdrop: scale the 320-wide image to ``wide_w`` PRESERVING aspect (nearest-neighbour — it's
    pixel art) and centre-crop the height overflow, so the parallax layer fills the wide frame as one coherent
    picture instead of edge-pixel smears. Only the gameplay VIEWPORT rows matter (the HUD strip below is
    overwritten with palette[0] by the caller); the returned image keeps the input's 200-row height."""
    in_h, in_w = backdrop.shape[:2]
    s = wide_w / in_w
    xs = np.minimum((np.arange(wide_w) / s).astype(np.intp), in_w - 1)
    vp_out = np.minimum(int(round(VIEWPORT_H * s)), in_h)     # scaled viewport height (>= VIEWPORT_H)
    top = (vp_out - VIEWPORT_H) // 2                          # centre-crop the vertical overflow
    ys = np.minimum(((np.arange(VIEWPORT_H) + top) / s).astype(np.intp), in_h - 1)
    out = np.empty((in_h, wide_w, 3), dtype=backdrop.dtype)
    out[:VIEWPORT_H] = backdrop[ys][:, xs]
    out[VIEWPORT_H:] = backdrop[VIEWPORT_H:, xs]              # HUD rows: placeholder (caller overwrites)
    return out
