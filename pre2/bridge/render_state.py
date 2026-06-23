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
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.hud_chrome import load_hud_chrome
from pre2.recovered.animation import AnimStep
from pre2.recovered.render_frame import ASSET_HI, ASSET_LO, FadeStep, IrisState, RendererState
from pre2.recovered.render_model import CameraShakeState, HudChromeAsset, HudState

_DS = 0x1A0F
_ANIM_FRAME_PTR = 0x6BC2   # [0x6BC2] = current animation-frame remap base
_ANIM_THROTTLE = 0x6BD4    # [0x6BD4] = per-frame throttle counter
_ANIM_ACTIVE = 0x6BBD      # [0x6BBD] = animated tiles present this frame
_ANIM_SPEED = 0x6BF6       # [0x6BF6] = scroll speed (>=0x14 doubles the cycle rate)
_SHAKE_MAG = 0x6BEA        # [0x6BEA] = camera-shake magnitude/timer (set 7/4 on landing, decays)
_FRAME_CTR = 0x6BD5        # [0x6BD5] = frame counter (its parity gates the shake's alternation)
_ROW_FACTOR = 0x6BF8       # [0x6BF8] = render row-stride factor; the shake apply (4C30) overwrites it
                           # with the magnitude on odd parity / 0 on even -> the applied vertical offset
_SCORE = 0x6C0E            # [0x6C0E]/[0x6C10] = 32-bit internal score (HUD shows it *10)
_LIVES = 0x27D8            # [0x27D8] = lives count
_ENERGY = 0x27D6           # [0x27D6] = energy (hearts)
_IRIS_RADIUS = 0x2DD0      # iris radius (low byte; shrinks each frame) — see bridge.transition
_IRIS_X = 0x2DC6           # iris circle centre X (player)
_IRIS_Y = 0x2DC8           # iris circle centre Y (player)


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


def _rws(mem, off):
    v = _rw(mem, off)
    return v - 0x10000 if v & 0x8000 else v


def _anim_step(mem) -> AnimStep:
    """Read the animated-tile cycle inputs at redraw (1030:367D): remap pointer, throttle
    counter, the animated-tiles-present gate, and the scroll speed."""
    return AnimStep(frame_ptr=_rw(mem, _ANIM_FRAME_PTR), throttle=_rb(mem, _ANIM_THROTTLE),
                    active=_rb(mem, _ANIM_ACTIVE) != 0, speed=_rw(mem, _ANIM_SPEED))


_HUD_CHROME_SEG = 0x3d    # [0x3d] = loaded HUD chrome segment (status-bar bitmap + glyph font), 0x252B
_HUD_BAR_OFF = 0x0B48     # status-bar bitmap offset within the chrome segment
_HUD_BAR_LEN = 0xE60      # 4 planes x 0x398
_HUD_FONT_LEN = 0x3000    # covers the glyph tables (glyph 0 at 0x1610 .. ~0x2E0F)


def _hud_chrome(mem) -> HudChromeAsset:
    """Fallback: capture the HUD chrome from the loaded chrome segment ([0x3d]=0x252B) in VM memory —
    the status-bar bitmap (0x0B48) and glyph font (0x1610). This is the *transient* runtime copy
    (reused after the level-start blit), so it is only valid on a level-start snapshot; prefer the
    persistent :func:`pre2.bridge.hud_chrome.load_hud_chrome` (ALLFONTS.SQZ) when a game_root is
    available. Segment/offset knowledge stays here."""
    base = (_rw(mem, _HUD_CHROME_SEG) << 4) & 0xFFFFF
    return HudChromeAsset(
        bar=bytes(mem.data[base + _HUD_BAR_OFF:base + _HUD_BAR_OFF + _HUD_BAR_LEN]),
        font=bytes(mem.data[base:base + _HUD_FONT_LEN]),
    )


def _hud_state(mem) -> HudState:
    """Read the status-bar values the HUD render (1030:45B8) draws: the score ([0x6C0E]/[0x6C10],
    a 32-bit count displayed *10), lives ([0x27D8]) and energy hearts ([0x27D6])."""
    score = (_rw(mem, _SCORE) | (_rw(mem, _SCORE + 2) << 16)) * 10
    return HudState(score=score, lives=_rb(mem, _LIVES), energy=_rb(mem, _ENERGY))


def _asset_planes(mem) -> tuple:
    """Capture the planar ASSET region (tile cache + parallax base, ASSET_LO..ASSET_HI) from each of
    the four EGA planes, so render_frame can render the background from a clean framebuffer. This is
    level asset data (built at load), not per-frame render output."""
    d = mem.data
    return tuple(bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE + ASSET_LO:
                         EGA_APERTURE + p * EGA_PLANE_STRIDE + ASSET_HI]) for p in range(4))


def _shake_state(mem) -> CameraShakeState:
    """Read the camera-shake-on-fall state. ``[0x6BEA]`` is the magnitude/timer and ``[0x6BD5]&1``
    the frame parity its alternation rides on. The apply is CONFIRMED: 1030:4C30 overwrites the
    render row-stride factor ``[0x6BF8]`` (== :attr:`RendererState.row_factor`, which ``render_frame``
    already consumes) with the magnitude on odd parity / 0 on even — a vertical viewport jolt of
    ``{0, magnitude}`` px (matched by pixel cross-correlation). ``applied_offset`` is that per-frame
    offset (0 when no shake is active)."""
    mag = _rb(mem, _SHAKE_MAG)
    return CameraShakeState(magnitude=mag, active=mag > 0, phase=_rb(mem, _FRAME_CTR) & 1,
                            applied_offset=(_rw(mem, _ROW_FACTOR) if mag > 0 else 0))


def _iris_state(mem):
    """Resolve the circular-iris transition state, or ``None`` if no iris is running.

    Discriminator: the radius byte ``[0x2DD0]`` is 0 outside the transition and runs 0xE6->0
    during it (the loop breaks at <=0). ``center_*`` are the signed circle centre the iris
    builder reads ([0x2DC6]/[0x2DC8])."""
    radius = _rb(mem, _IRIS_RADIUS)
    if radius == 0:
        return None
    return IrisState(radius=radius, center_x=_rws(mem, _IRIS_X), center_y=_rws(mem, _IRIS_Y))


def read_renderer_state(mem, dos=None, *, game_root=None, frame_pre_inc: bool = True) -> RendererState:
    """Snapshot every renderer input from VM memory into a plain :class:`RendererState`.

    ``frame_pre_inc`` matches the object renderer's +1 to [0x6BD5] applied at 26FA entry
    (capture this state *before* that increment to see the value the engine will use).
    Pass ``dos`` to also capture the full palette state machine (displayed DAC colours +
    phase + base index) via :func:`pre2.bridge.palette.read_palette_state`; without it the
    snapshot carries only the fade step (no displayed colours).

    Pass ``game_root`` (the assets dir) to source the static HUD chrome from its persistent asset
    (``ALLFONTS.SQZ``) via :func:`pre2.bridge.hud_chrome.load_hud_chrome`, so the HUD renders from
    *any* snapshot; without it the chrome falls back to the transient in-VM copy (:func:`_hud_chrome`,
    valid only on a level-start snapshot)."""
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
        palette=(_pal.read_palette_state(mem, dos) if dos is not None else None),
        iris=_iris_state(mem),
        anim=_anim_step(mem),
        shake=_shake_state(mem),
        hud_state=_hud_state(mem),
        # Prefer the persistent HUD chrome asset (panel + font from ALLFONTS.SQZ); fall back to the
        # transient in-VM copy when no game_root is given (valid only on a level-start snapshot).
        hud_chrome=(load_hud_chrome(game_root) if game_root is not None else _hud_chrome(mem)),
        asset_planes=_asset_planes(mem),
        object_camera=obj_cam,
        object_sprites=obj_sprites,
        object_attrs=obj_attrs,
        object_src_banks=obj_banks,
    )
