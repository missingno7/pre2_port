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
from pre2.bridge.object_render import read_attr as _read_attr
from pre2.bridge.render_state import read_renderer_state
from time import perf_counter as _perf

from pre2.enhanced.frame_state import EnhancedFrameState, SpriteInstance
from pre2.enhanced.native_background import (NativeBackgroundUnsupported, TileTextureCache, _HudCache, hud_pad as _hud_pad,
                                             native_background_indices)
from pre2.enhanced.sprite_cache import SpriteTexture, SpriteTextureCache, palette_version
from pre2.recovered.object_render import (LIST_TOP, MODE_ERASE, MODE_NORMAL, MODE_OPAQUE, RECORD_BYTES, _s8, _s16,
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
_WORLD_W_PX = 0x1000                     # world width: 256 tilemap columns (player X_MIN/X_MAX world bounds)
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


def _extract_particles(pf, m_left=0, m_right=0):
    """Lift the one-shot point particles (4B8E) to interpolatable points: ``(screen_x, screen_y, vel_x,
    vel_y)`` for each on-screen particle, matching draw_particles' advance + cull + screen mapping exactly
    (so at alpha=1 the compositor plots the same pixel). vel is the particle's per-frame world delta (=
    screen delta), used to rewind it along its own path for sub-source-frame motion.

    ``m_left``/``m_right`` widen the X cull to the WIDESCREEN window: the returned ``screen_x`` is still
    relative to the 320-window left (so the caller's ``+ m_left`` lands it in the wide buffer); at margin 0
    the cull is exactly the faithful ``sx >= 0x140`` (spider threads pop out at the faithful edge otherwise —
    the left margin wrapped negative and the right margin exceeded 320, so both edges dropped them)."""
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
        sx = sx - 0x10000 if sx >= 0x8000 else sx       # SIGNED screen X rel to the 320-window left edge
        if not (-m_left <= sx < 0x140 + m_right):       # off left/right (wide window)
            continue
        vx = ((nx - x + 0x8000) & 0xFFFF) - 0x8000      # signed per-frame delta
        vy = ((ny - y + 0x8000) & 0xFFFF) - 0x8000
        pts.append((sx, sy, vx, vy))
    return pts


def _extract_fireflies(ff, m_left=0, m_right=0):
    """Lift the persistent firefly swarm (54AB) to interpolatable points: ``(slot, world_x, world_y,
    screen_x, screen_y)`` for each on-screen firefly, matching draw_fireflies' screen mapping exactly
    (so at alpha=1 the compositor plots the same pixel). ``slot`` is the persistent slot index used to
    match prev/cur and lerp the world position; ``world = (x>>3, y>>3)`` (the camera-relative draw uses
    those shifted coords). ``m_left``/``m_right`` widen the X cull to the widescreen window (screen_x stays
    relative to the 320-window left; caller adds m_left); margin 0 == the faithful ``sx >= 0x140`` cull."""
    cam_x = (ff.cam_col << 4) & 0xFFFF
    cam_y = (ff.cam_row << 4) & 0xFFFF
    pts = []
    for idx, (x, y, _timer) in zip(ff.slot_idx or range(len(ff.slots)), ff.slots):
        wx, wy = _sar(x, 3), _sar(y, 3)
        sy = (wy - cam_y) & 0xFFFF
        if sy >= 0xB0:
            continue
        sx = (wx - cam_x) & 0xFFFF
        sx = sx - 0x10000 if sx >= 0x8000 else sx       # SIGNED screen X rel to the 320-window left edge
        if not (-m_left <= sx < 0x140 + m_right):
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


def _replan_wide(spr, attr, cam, m_left, m_right, v_pad=0):
    """WIDE-WINDOW re-admission (Experimental — true widescreen X margins + the smooth camera's vertical
    ``v_pad`` band): the faithful planner culls a record whose sprite lies outside the original 320x176 window;
    when the record exists in the render list but only fails the window cull, replan it as if it were on-screen
    and give back its real placement. READ-ONLY — the game's state (including the render list) is never
    touched; this only widens the presentation cull, so record/state digests stay byte-exact. The trick: shift
    the camera by WHOLE TILES (preserving ``screen_x & 7``; Y has no sub-tile raster dependency) to bring the
    sprite into the faithful window, plan there (mode/blink/flip all evaluated by the UNMODIFIED recovered
    planner), then shift the resulting placement back. Returns ``(cmd, k_tiles, ky_tiles)`` or None (record
    empty / outside the wide+tall window too).

    ``v_pad`` extends the Y window by that many px each side (the smooth camera's vertical deviation band,
    == the tile over-extraction): the faithful Y cull drops baseline<=0 / top>=176, the extended window keeps
    everything with baseline > -v_pad and top < 176+v_pad. v_pad=0 -> the faithful Y cull, unchanged."""
    if spr.sprite_id == 0xFFFF:
        return None
    x_off = attr.width - attr.x_off if (spr.sprite_id & 0x8000) else attr.x_off
    sx = _s16(spr.x - x_off - cam.cam_x * 16)               # the pre-cull screen X [asm 277A..27B5]
    if not (-(attr.width + m_left) < sx < 320 + m_right):   # outside even the WIDE window
        return None
    k = (sx - 152) // 16                                    # tile shift landing sx' in [152,167] (mid-window)
    ky = 0
    if v_pad:
        # the pre-cull screen Y BASELINE [asm 27B8..27D9]; cull rules: baseline <= 0 (off top) or
        # baseline - height >= 176 (off bottom). Extended by v_pad each side:
        sy = _s16(spr.y + _s8(attr.y_off) + cam.row_factor - (cam.cam_y * 16 + cam.fine_scroll))
        if sy <= -v_pad or sy - attr.height >= 176 + v_pad:  # outside even the TALL window
            return None
        ky = (sy - 96) // 16                                # tile shift landing sy' in [96,111] (mid-viewport)
    cmd = plan_sprite_command(spr, attr, replace(cam, cam_x=cam.cam_x + k, cam_y=cam.cam_y + ky))
    if cmd is None:                                          # Y-culled (v_pad=0) or empty even mid-window
        return None
    return replace(cmd, screen_x=cmd.screen_x + 16 * k, screen_y=cmd.screen_y + 16 * ky), k, ky


from pre2.recovered.object_render import Sprite as _Sprite

# The two per-frame projectors (terrain platforms 4907 -> render slots 0x5570, float-effect/pickup items 8922 ->
# render slots 0x52E8) cull each SOURCE entity to the faithful 320 window BEFORE writing the render-slot array
# that object_render reads. So a platform or floating item in the WIDESCREEN MARGIN is dropped at the state
# projection and can NEVER be re-admitted by _replan_wide (it isn't in the active list at all). This read-only
# pass re-walks the two SOURCE lists and synthesizes active-list Sprite records for the entities that were NOT
# projected, so the sprite loop textures + _replan_wide-places them in the margins like enemies.
#
# "Not projected" is decided from the ACTUAL render slots (each slot's [+9] = the source offset it came from),
# NOT by recomputing the faithful X cull. The projectors run mid-tick (8922 @0235) BEFORE the scroll advances
# the camera, so a recomputed cull using the post-tick camera disagrees with them by up to one tile at the
# boundary -- a boundary item then falls through BOTH (the projector's camera said "margin", ours said "the
# projector has it") and BLINKS one frame as it crosses the 320 edge. Reading the render slots is exact and
# phase-proof. Parity is preserved by the sprite loop: a re-projected record is only DRAWN when plan_sprite_command
# culls it at 320 (it's in a margin) -- one that plans inside 320 (a budget-dropped central item) is dropped, so
# the central 320 never diverges from faithful. DS-relative reads only; the game state is never touched.
_TERRAIN_SRC, _TERRAIN_STRIDE, _TERRAIN_N = 0x9107, 0xF, 0x10      # [asm 4907] source list
_TERRAIN_DST, _TERRAIN_DST_N = 0x5570, 7                          # its render slots (stride 0x12, [+9]=source)
_FLOAT_SRC, _FLOAT_STRIDE, _FLOAT_N = 0x8F1D, 7, 0x46             # [asm 8922] source list
_FLOAT_DST, _FLOAT_DST_N = 0x52E8, 0x14                           # its render slots (stride 0x12, [+9]=source)
_DST_STRIDE = 0x12
# 2nd-pass entity list (6913 walker): variable stride at [si], ends at stride >= 0x32; entry 0 = the player.
_ENT2_LO, _ENT2_HI = 0x8489, 0x8F1D          # the list's address span (ends before the 0x8F1D float list)
_ENT2_STRIDE_END = 0x32                       # [6916] stride >= this terminates the walk
_ENT2_SELF_POS_IDX = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11})   # handlers that project at the entity's OWN pos


def _reproject_wide_entities(mem, cam):
    """Synthesize active-list :class:`Sprite` records for the terrain-platform + float-item SOURCE entities that
    the faithful projectors did NOT write into their render slots this frame (i.e. the margin entities). Returns
    ``[(Sprite, handle), ...]``; the handle is keyed to the stable SOURCE-entry offset (the same identity the
    projected records carry in slot[+9]) so interpolation matches the entity across frames and across the
    margin<->faithful hand-off. The sprite loop culls/places each via plan_sprite_command + _replan_wide."""
    d = mem.data
    base = 0x1A0F << 4

    def rw(off):
        return d[base + (off & 0xFFFF)] | (d[base + ((off + 1) & 0xFFFF)] << 8)

    def drawn_sources(dst, n):
        """The set of SOURCE offsets the projector actually wrote this frame ([+9] of each live render slot)."""
        s = set()
        for j in range(n):
            slot = dst + j * _DST_STRIDE
            if rw(slot + 4) != 0xFFFF:                           # live slot (not the 0xFFFF terminator/dead)
                s.add(rw(slot + 9))
        return s

    out = []
    # terrain platforms (4907): not-drawn source entities -> synthesize. Y-nudge -2 (the projector's non-ridden
    # nudge; a margin platform is never ridden by the centred player).
    drawn = drawn_sources(_TERRAIN_DST, _TERRAIN_DST_N)
    for k in range(_TERRAIN_N):
        si = _TERRAIN_SRC + k * _TERRAIN_STRIDE
        if rw(si + 4) == 0xFFFF or si in drawn:
            continue
        out.append((_Sprite(x=rw(si), y=(rw(si + 2) - 2) & 0xFFFF, sprite_id=rw(si + 4), flags=0, life=0),
                    ("terrain", si)))
    # float-effect / pickup items (8922): not-drawn source entities -> synthesize at their SOURCE Y as-is. The
    # 8922 float bounce advances the source [+2] only on PROJECTED (on-screen) frames, so a margin item's Y is
    # frozen at exactly the value the on-screen path would resume its bounce FROM -- so a pickup crossing the 320
    # edge hands off render-slot<->re-projection SEAMLESSLY (both read source [+2]). (An earlier presentation-side
    # bounce here animated the margin items but ran a DIFFERENT phase from the on-screen faithful bounce, so they
    # jumped at the boundary = the reported flicker; frozen-then-resume matches the original.)
    drawn = drawn_sources(_FLOAT_DST, _FLOAT_DST_N)
    for k in range(_FLOAT_N):
        si = _FLOAT_SRC + k * _FLOAT_STRIDE
        if rw(si + 4) == 0xFFFF or si in drawn:
            continue
        out.append((_Sprite(x=rw(si), y=rw(si + 2), sprite_id=rw(si + 4), flags=0, life=0), ("fx", si)))

    # 2nd-pass ENEMIES (6913 walker -> project_entity 7F26 into the shared pool 0x4FD0): these dormant enemies
    # (cave spiders etc.) are activated/drawn only when on-screen (on_screen_tile 8022 cull), so a margin enemy
    # pops in at the 320 edge. Re-project the not-yet-projected ones (read-only) at their own X[+9]/Y[+0xB]/
    # sprite[+2]. "Not projected" = its offset is not a back-ref [+6] of any live pool record. ONLY handlers that
    # draw the entity at its OWN position (idx 1-9,11 = the on_screen_tile projectors) qualify; idx 0 (a
    # player-relative aura), 10 (player trail) and 12 (proximity, its own gate/mode) draw elsewhere -> left to
    # the tick. The parity guard in the sprite loop drops any that plan inside 320 (never diverges the centre).
    d_rb = mem.data
    ent_drawn = set()
    for k in range(0x40):                                        # pool 0x4FD0, 0x40 slots, [+6] = source back-ref
        po = 0x4FD0 + k * 0x12
        if rw(po + 4) != 0xFFFF:
            bp = rw(po + 6)
            if _ENT2_LO <= bp < _ENT2_HI:
                ent_drawn.add(bp)
    b198 = d_rb[base + 0xB198]
    si = _ENT2_LO
    for _ in range(0x40):                                        # variable stride; bounded loop as a backstop
        stride = d_rb[base + si]
        if stride >= _ENT2_STRIDE_END:                           # [6916] end of the list
            break
        flags1 = d_rb[base + si + 1]
        sprite = rw(si + 2)
        skip = (sprite == 0xFFFF or (d_rb[base + si + 4] & 4) or (b198 != 1 and (flags1 & 0x80)))
        if (not skip and si != _ENT2_LO                          # entry 0 = the player (drawn as 0x4F1C)
                and (flags1 & 0x7F) in _ENT2_SELF_POS_IDX and si not in ent_drawn):
            out.append((_Sprite(x=rw(si + 9), y=rw(si + 0xB), sprite_id=sprite, flags=0, life=0), ("ent", si)))
        si += stride
    return out


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
                           wide_cull=False, hud_align="center", bg_mode="stretch",
                           bd_pad=0, slide_margins=None, room_mode=False, v_pad=0) -> EnhancedFrameState | None:
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
    shifted by the left margin; the backdrop fits to the wide width (scale + centre-crop height); the HUD
    strip (no wider source exists) extends by edge-pixel replication; beyond-the-world pixels render black.
    The central 320 columns are IDENTICAL to margin=0 (widescreen only ADDS pixels outside the faithful
    window — except the backdrop, which is fit-to-width scaled); entities fully outside the original window
    pop in at its edge (the game never emits render records for them) — the standard widescreen-mod artifact.
    margin=0 (the default and the parity path) is byte-identical to the pre-widescreen extractor.
    ``wide_cull`` (TRUE WIDESCREEN, Experimental) re-admits render records the faithful planner culls at the
    320px boundary, so entities in the margins draw instead of popping in/out at the faithful edge. Read-only
    (state digests stay byte-exact) but the PRESENTATION deliberately shows what the original would not.
    Only records the game's own producers emitted are covered — entities beyond the producers' window still
    pop in at the wide edge. It also SLIDES the margin split near the world edges (left margin shrinks to 0
    at the level start, the right grows by the same amount) so the wide window stays inside the world and no
    beyond-the-world void shows. Without it the split is symmetric and beyond-the-world pixels render BLACK.
    """
    # ``slide_margins`` decouples the world-edge margin SLIDE from the rest of wide_cull: the SMOOTH CAMERA passes
    # wide_cull=True (entities still re-admit in the margins) with slide_margins=False (its crop needs a symmetric
    # split, and it respects the world edge via its own presentation clamp). None -> follow wide_cull (normal cam).
    # ``room_mode`` (LEVEL6 tower bands / LEVEL F single-screen): black-void everything outside the CURRENT 320
    # window -- the room renders centred with black margins (the neighbouring tilemap columns are other rooms/
    # bands, so revealing them is wrong). The caller passes wide_cull=False with it (no margin content to re-admit).
    if slide_margins is None:
        slide_margins = wide_cull
    rs = read_renderer_state(mem, dos, game_root=game_root)
    cam = rs.object_camera
    if cam is None:
        return None
    page, stride = cam.dest_page, cam.row_stride
    palette = dos.vga_palette or [(0, 0, 0)] * 256
    pal_rgb = np.asarray(palette, dtype=np.uint8)

    # Widescreen margin SPLIT: normally symmetric (margin each side). Under wide_cull (true widescreen) the
    # split slides so the window stays within the WORLD (256 tiles = [0, 0x1000) px — the X_MIN/X_MAX world
    # of player.py): pinned at a world edge the window stops moving while the faithful camera keeps going,
    # exactly like a wider camera clamp. ``m_left`` replaces the symmetric ``margin`` in every screen-space
    # placement below; with wide_cull off (or margin 0) m_left == m_right == margin — behavior unchanged.
    m_left = m_right = margin
    cam_px = cam.cam_x * 16
    if margin and slide_margins:
        m_left = min(margin, cam_px)                                    # don't extend past the world's left
        m_left = max(m_left, 2 * margin - max(0, _WORLD_W_PX - (cam_px + 320)))   # borrow when pinned right
        m_left = min(max(m_left, 0), 2 * margin)
        m_right = 2 * margin - m_left

    # Backdrop = the FIXED parallax base layer (sky/mountains), de-planarized directly from 0x7E80.
    # Widescreen: the backdrop is a fixed 320px image with no wider source -> FIT TO WIDTH: scale the viewport
    # portion up to the wide width (nearest-neighbour, preserving aspect) and centre-crop the height overflow.
    backdrop_rgb = _render_backdrop(rs, page, palette)
    wide_w = 320 + m_left + m_right                       # constant across the level (m_left+m_right == 2*margin)
    if margin:
        # ``bd_pad`` (the smooth-camera CROP margin): the backdrop is SCREEN-FIXED, and the presenter's crop
        # always removes exactly bd_pad from each side — so fit the backdrop to the DISPLAY width (keeping the
        # central image pixel-native for mirror/black and correctly proportioned for stretch) and edge-pad the
        # never-displayed crop margins.
        backdrop_rgb = _widescreen_backdrop(backdrop_rgb, wide_w - 2 * bd_pad, bg_mode)
        if bd_pad:
            backdrop_rgb = np.pad(backdrop_rgb, ((0, 0), (bd_pad, bd_pad), (0, 0)), mode="edge")

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
    idx_ext = None
    try:
        idx0 = native_background_indices(rs, tile_cache, hud_cache, m_left, m_right, hud_align, bd_pad,
                                         v_pad=v_pad)
        if v_pad:
            idx0, idx_ext = idx0
    except NativeBackgroundUnsupported:
        tile_cache.stats["fallbacks"] += 1
        bg0_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes)),
                     bg0_planes, palette, rebuild=True)
        idx0 = render_planar_rgb_from_planes(bg0_planes, page, _ID_PAL)[:, :, 0]
        if margin:                                       # faithful fallback is 320-wide -> edge-extend the
            vp = np.pad(idx0[:VIEWPORT_H], ((0, 0), (m_left, m_right)), mode="edge")   # viewport by margins,
            pl, pr = _hud_pad(320 + m_left + m_right, hud_align, bd_pad)                # the HUD by its own align
            idx0 = np.concatenate([vp, np.pad(idx0[VIEWPORT_H:], ((0, 0), (pl, pr)), mode="edge")], axis=0)
    tile_cache.stats["native_s"] += _perf() - _t0
    tile_mask = idx0 != 0
    backdrop_full = backdrop_rgb.copy()
    backdrop_full[VIEWPORT_H:] = pal_rgb[0]                   # HUD rows: base-showing == palette[0] (panel bg)
    background_rgb = np.where(tile_mask[..., None], pal_rgb[idx0], backdrop_full)
    tile_ext_rgb = tile_ext_mask = None
    if idx_ext is not None:                                   # the smooth camera's vertical over-extraction
        tile_ext_mask = idx_ext != 0
        tile_ext_rgb = pal_rgb[idx_ext]
    if margin:
        # BEYOND-THE-WORLD void: wide pixels mapping outside the 256-tile world render BLACK (not tilemap
        # edge repeats, not backdrop). Marked as tile coverage so the compositor scrolls the void with the
        # world (it is world-anchored). With the wide_cull slide the window stays inside the world -> empty.
        # ROOM MODE (LEVEL6 tower / LEVEL F single-screen): the level is a 320-wide ROOM inside the 256-tile
        # backing map -- the columns beside it belong to OTHER rooms/bands, so revealing them is wrong. The
        # void covers everything outside the CURRENT faithful window instead: the room renders centred in the
        # wide frame with pure black each side ("see outside the level border, filled with black").
        wx_world = np.arange(wide_w) + (cam_px - m_left)      # wide pixel column -> world pixel X
        w_lo, w_hi = (cam_px, cam_px + 320) if room_mode else (0, _WORLD_W_PX)
        void = (wx_world < w_lo) | (wx_world >= w_hi)
        if void.any():
            background_rgb[:VIEWPORT_H, void] = 0
            tile_mask[:VIEWPORT_H, void] = True
            if tile_ext_rgb is not None:                      # keep the ext layer's void identical (all rows)
                tile_ext_rgb[:, void] = 0
                tile_ext_mask[:, void] = True

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
        if margin:                                       # screen-space records: place at +m_left, empty margins
            idx_ov = np.pad(idx_ov, ((0, 0), (m_left, m_right)))
        overlay_mask = idx_ov != 0
        overlay_rgb = pal_rgb[idx_ov]
        # TRUE WIDESCREEN (wide_cull) admits particles/fireflies into the margins too (else spider threads +
        # sparkles pop out at the faithful edge like the culled sprites did); plain widescreen keeps them in
        # the central 320 (margins are backdrop-extension only). margin 0 -> both cull args 0 -> parity-exact.
        cull_l = m_left if wide_cull else 0
        cull_r = m_right if wide_cull else 0
        if effects.particles is not None:
            particles = [(sx + m_left, sy, vx, vy)
                         for (sx, sy, vx, vy) in _extract_particles(effects.particles, cull_l, cull_r)]
            particle_rgb = tuple(int(c) for c in pal_rgb[15])    # 4B8E plots colour 15 (white)
        if effects.fireflies is not None:
            fireflies = [(i, wx, wy, sx + m_left, sy)
                         for (i, wx, wy, sx, sy) in _extract_fireflies(effects.fireflies, cull_l, cull_r)]
            firefly_rgb = tuple(int(c) for c in pal_rgb[15])     # VM oracle collapses the 14/15 flicker to 15

    faithful_rgb = None
    if with_faithful:
        full_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(rs, full_planes, palette, rebuild=True)
        faithful_rgb = render_planar_rgb_from_planes(full_planes, page, palette)

    sprites, unsupported = [], []
    attrs = rs.object_attrs or {}
    banks = rs.object_src_banks or {}
    # TRUE WIDESCREEN: re-project the terrain-platform + float-item entities the state projector X-culled to the
    # faithful 320 window, so they draw in the margins (like enemies, which stay in the active list). Each is an
    # (index, Sprite, handle) triple with a synthetic slot (identity comes from the explicit handle, not the
    # record offset); attrs/banks are extended for any id the culled entities reference. margin 0 / no wide_cull
    # -> the list is empty and the loop is byte-identical to before.
    extra_sprites = []
    if (wide_cull and margin) or v_pad:                      # X margins (true widescreen) / Y band (smooth cam)
        reproj = _reproject_wide_entities(mem, cam)
        if reproj:
            attrs = dict(attrs)                                  # copy: never mutate the shared render-state dicts
            banks = dict(banks)
            for spr, handle in reproj:
                a = attrs.get(spr.sprite_id)
                if a is None:
                    a = attrs[spr.sprite_id] = _read_attr(mem, spr.sprite_id)
                if a.src_seg not in banks:
                    lo = (a.src_seg << 4) & 0xFFFFF
                    banks[a.src_seg] = bytes(mem.data[lo:lo + 0x10000])
                extra_sprites.append((spr, handle))
    # Sprite texture cache (layer A): the palette-INDEPENDENT cel textures are reused across source frames when
    # the session passes a persistent ``tex_cache`` (steady gameplay re-extracts only cels that actually
    # changed), else a throwaway cache (the parity path -> identical output, just no cross-frame reuse). The
    # palette is applied per frame, so fades never invalidate the cache.
    cache = tex_cache if tex_cache is not None else SpriteTextureCache()
    pversion = palette_version(palette)
    # camera in PIXELS, matching the sprite placement: X = the WIDE WINDOW's world-left (cam_x*16 - m_left —
    # with a fixed split that differs from cam_x*16 only by a constant, so deltas are unchanged; with the
    # wide_cull slide it PINS at world edges so the compositor correctly stops scrolling there);
    # Y = cam_y*16 + fine_scroll - row_factor (screen_y = world_y + y_off + row_factor - (cam_y*16 + fine) —
    # the SHAKE bias [0x6BF8] is part of the effective camera; including it makes the camera delta carry the
    # shake and the WHOLE frame shakes between subframes, not just the sprites). Used to interpolate the
    # background scroll between ticks.
    camera_px = (cam_px - m_left, cam.cam_y * 16 + cam.fine_scroll - cam.row_factor)
    # enumerate -> `slot` is the active-list record index (stable cross-frame identity, animation-independent).
    # The re-projected margin entities follow with slot=None (identity via their explicit handle); they are ALL
    # X-culled at 320 (only margin entities are re-projected), so they route through _replan_wide like enemies.
    real = ((slot, spr, None) for slot, spr in enumerate(rs.object_sprites or ()))
    for slot, spr, ext_handle in (*real, *((None, s, h) for s, h in extra_sprites)):
        attr = attrs.get(spr.sprite_id)
        if attr is None:
            continue
        cmd = plan_sprite_command(spr, attr, cam)
        kshift = kyshift = 0
        if cmd is None:
            if (wide_cull and margin) or v_pad:             # re-admit a window-culled record: the widescreen X
                #   margins and/or the smooth camera's vertical deviation band (both presentation-only widenings)
                res = _replan_wide(spr, attr, cam, m_left, m_right, v_pad)
                if res is None:
                    continue
                cmd, kshift, kyshift = res
            else:
                continue
        elif ext_handle is not None:
            # A re-projected entity that plans INSIDE the 320 window = a source item the faithful projector did
            # not draw yet IS within the central view (budget-drop / a mid-tick-camera boundary item now central).
            # Faithful doesn't draw it, so neither do we -- the central 320 must stay byte-exact with faithful.
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
        # For a re-admitted (wide/tall-window) record, plan the raster geometry at the SHIFTED camera (the
        # sprite is mid-window there -> unclipped); the texture key/bake are position-independent.
        draw = plan_sprite(spr, attr, cam if kshift == 0 and kyshift == 0
                           else replace(cam, cam_x=cam.cam_x + kshift, cam_y=cam.cam_y + kyshift))
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
        wx, wy = cmd.world_x, cmd.world_y
        if ext_handle is not None:                               # a re-projected margin entity: identity is its
            sprites.append(SpriteInstance(handle=ext_handle, slot=-1, base_id=cmd.base_id,   # explicit handle,
                                          sprite_id=cmd.sprite_id, world_x=wx, world_y=wy,    # no active-list rec
                                          screen_x=cmd.screen_x + m_left, screen_y=cmd.screen_y,
                                          tex_off_x=tex.off_x, tex_off_y=tex.off_y,
                                          rgba=rgba, interpolate=not cmd.is_hud))
            continue
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
                                      screen_x=cmd.screen_x + m_left, screen_y=cmd.screen_y,
                                      tex_off_x=tex.off_x, tex_off_y=tex.off_y,
                                      rgba=rgba, interpolate=not cmd.is_hud))
    return EnhancedFrameState(background_rgb=background_rgb, camera=camera_px,
                              cam_margin_left=m_left, row_factor=cam.row_factor,
                              sprites=sprites, faithful_rgb=faithful_rgb, unsupported=unsupported,
                              backdrop_rgb=backdrop_rgb, tile_mask=tile_mask,
                              tile_ext_rgb=tile_ext_rgb, tile_ext_mask=tile_ext_mask,
                              v_pad=v_pad if tile_ext_rgb is not None else 0,
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


def _resize_linear(img, n_out: int, axis: int):
    """Separable linear (bilinear per-axis) resize of a float32 image along ``axis`` (edge-clamped)."""
    n_in = img.shape[axis]
    pos = (np.arange(n_out, dtype=np.float32) + 0.5) * (n_in / n_out) - 0.5
    lo = np.clip(np.floor(pos).astype(np.intp), 0, n_in - 1)
    hi = np.minimum(lo + 1, n_in - 1)
    w = (pos - lo).astype(np.float32)
    shape = [1] * img.ndim
    shape[axis] = n_out
    w = w.reshape(shape)
    return np.take(img, lo, axis=axis) * (1.0 - w) + np.take(img, hi, axis=axis) * w


_BD_FIT_CACHE: dict = {}                 # (wide_w, backdrop-bytes hash) -> scaled uint8 (bounded)


def _widescreen_backdrop(backdrop, wide_w: int, mode: str):
    """Fill the widescreen backdrop (parallax sky/mountains — a fixed 320-wide image with no wider source) to
    ``wide_w``, per ``mode``. The parallax is screen-FIXED, so the native image is ALWAYS CENTRED (equal
    margins each side) and STATIC across the level — it never slides with the wide_cull margin split (which is
    a tile-layer/gameplay-window concern, not the backdrop's):
      * ``stretch`` — fit the whole 320 image to the full width (sharp-bilinear zoom); edge-to-edge, zoomed.
      * ``mirror``  — native centred, each margin a MIRROR reflection of the adjacent edge (seamless).
      * ``black``   — native centred, black margins (cinematic).
    ``mirror`` / ``black`` keep the central 320 pixel-native."""
    if wide_w <= 320:                                         # no margins (smooth-camera bd_pad only) -> native
        return backdrop
    if mode == "stretch":
        return _fit_backdrop_width(backdrop, wide_w)
    in_h = backdrop.shape[0]
    left = (wide_w - 320) // 2                                # symmetric, centred (wide_w-320 == 2*margin, even)
    right = wide_w - 320 - left
    out = np.zeros((in_h, wide_w, 3), dtype=backdrop.dtype)   # black margins by default
    out[:, left:left + 320] = backdrop
    if mode == "mirror":
        if left > 0:                                          # reflect about the left edge of the native image
            out[:, :left] = backdrop[:, :left][:, ::-1]
        if right > 0:                                         # reflect about the right edge
            out[:, left + 320:] = backdrop[:, 320 - right:][:, ::-1]
    return out


def _fit_backdrop_width(backdrop, wide_w: int):
    """Widescreen backdrop: fit the 320-wide image to ``wide_w`` PRESERVING aspect and centre-crop the height
    overflow, using SHARP-BILINEAR scaling (integer nearest-neighbour prescale x2, then bilinear down to the
    target — the emulator-style sharp scaler): pixels keep crisp, EVENLY-sized edges instead of raw NN's
    uneven column widths at fractional ratios. Cached on the backdrop's content (it changes only on level
    load / palette fade), so the scale cost is off the steady-state hot path. Only the gameplay VIEWPORT rows
    matter (the HUD strip below is overwritten with palette[0] by the caller); the returned image keeps the
    input's 200-row height."""
    in_h, in_w = backdrop.shape[:2]
    vp = np.ascontiguousarray(backdrop[:VIEWPORT_H])
    key = (wide_w, hash(vp.tobytes()))
    hit = _BD_FIT_CACHE.get(key)
    if hit is None:
        s = wide_w / in_w
        up = np.repeat(np.repeat(vp.astype(np.float32), 2, axis=0), 2, axis=1)   # NN x2 prescale
        out_h = max(VIEWPORT_H, int(round(VIEWPORT_H * s)))
        down = _resize_linear(_resize_linear(up, out_h, 0), wide_w, 1)           # bilinear to target
        top = (out_h - VIEWPORT_H) // 2                                          # centre-crop the overflow
        hit = np.clip(down[top:top + VIEWPORT_H] + 0.5, 0, 255).astype(np.uint8)
        if len(_BD_FIT_CACHE) > 8:                                               # bounded (palette fades churn)
            _BD_FIT_CACHE.clear()
        _BD_FIT_CACHE[key] = hit
    out = np.empty((in_h, wide_w, 3), dtype=backdrop.dtype)
    out[:VIEWPORT_H] = hit
    out[VIEWPORT_H:] = 0                                      # HUD rows: placeholder (caller overwrites)
    return out
