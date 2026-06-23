"""Prehistorik 2 — the semantic render model (render *intent*, not VGA pixels).

This is the abstraction the faithful renderer should *emit* and the future enhanced
renderer should *consume*. It sits between the recovered renderer leaves and the planar
rasteriser:

    VM / oracle framebuffer
      -> recovered video routines (object_render, frame_renderer, transition, present)
      -> RENDER MODEL (this module): a GameFrameSnapshot of render commands  <-- the seam
      -> faithful rasteriser (the recovered leaves -> EGA planes)   [verified == ASM]
      -> future enhanced renderer (own framebuffer, own clock, interpolation)

The commands carry *meaning* — object identity, world/screen position, sprite id, palette
and transition state, draw order — independent of how VGA realises them (planar offsets,
the scroll ring buffer, the page flip). Those machine details stay in the faithful
rasteriser as oracle/scaffolding; they must not leak into this model.

Everything here is plain, frozen data: no ``cpu``/``mem``/``dos_re``, no plane buffers, no
VRAM offsets. A ``GameFrameSnapshot`` is the unit two consecutive captures can be
interpolated between (camera + per-object positions are explicit pixel coordinates).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

TILE_PX = 16
SCREEN_W = 320
SCREEN_H = 200


class BlitMode(IntEnum):
    """Why/how a sprite is painted this frame (the blink/erase/opaque decision)."""
    ERASE = 0x00     # blink "off" frame: mask/erase only
    NORMAL = 0x01    # mask + sprite
    OPAQUE = 0x10    # sprite-only, monochrome all-plane OR


class TransitionKind(IntEnum):
    NONE = 0
    IRIS = 1         # circular iris/vignette closing toward the player
    FADE = 2         # linear palette fade


@dataclass(frozen=True)
class CameraState:
    """The frame's camera as explicit pixel coordinates (interpolation-ready).

    ``x_px``/``y_px`` are the world-space top-left of the view in pixels
    (``cam_tiles*16 + fine_scroll``); two snapshots' cameras can be lerped directly.
    """
    x_px: int
    y_px: int
    cam_tile_x: int   # the faithful tile camera (oracle bookkeeping kept for verification)
    cam_tile_y: int
    fine_scroll: int


@dataclass(frozen=True)
class SpriteDrawCmd:
    """One sprite's render intent: *what* object, *where*, *which* graphic, *how*.

    World + screen positions are explicit pixels (no VRAM offsets). ``base_id`` is the
    stable identity for matching the same object across frames (``sprite_id & 0x1FFF``);
    ``flip`` and ``mode`` are the graphic/blink intent; ``life`` is the animation/blink
    phase. ``src_seg``/``src_off``/``width``/``height`` identify the sprite pixels. The
    planar realisation (dest offset, post-shift byte width, edge clips) is NOT here — it
    lives in ``object_render.SpriteDraw`` (the faithful rasteriser's command).
    """
    sprite_id: int        # full id (incl. flip bit15 / opaque bit14)
    base_id: int          # sprite_id & 0x1FFF — the cross-frame identity
    flip: bool            # horizontal flip
    mode: BlitMode        # erase / normal / opaque (the blink/anim decision this frame)
    life: int             # animation/life counter (blink phase)
    world_x: int          # world position (px) — from the object record
    world_y: int
    screen_x: int         # left edge on screen (px); may be negative (left-clipped)
    screen_y: int         # top edge on screen (px); may be negative (top-clipped)
    width: int            # sprite width (px, pre-shift)
    height: int           # sprite height (rows)
    src_seg: int          # sprite pixel-data segment + offset (the graphic)
    src_off: int
    is_hud: bool = False   # fixed-screen element (id 0x135 path): no camera, no interp


@dataclass(frozen=True)
class TileDrawCmd:
    """One background tile's render intent: which tile graphic at which grid cell / screen
    position, with its attribute class. (Contract; the tile-background lift is the next
    step — today the faithful renderer still rasters the background via the scroll ring.)
    """
    tile_id: int
    grid_col: int
    grid_row: int
    screen_x: int
    screen_y: int
    type_attr: int        # transparency/animation class (type_tbl/blit_type)


class FadePhase(IntEnum):
    """The palette-fade state machine's phase (renderer-owned, evolves each frame)."""
    NONE = 0
    IN = 1       # fading toward the target ([0x6C02] direction 0)
    OUT = 2      # fading back ([0x6C02] direction 1)


@dataclass(frozen=True)
class PaletteState:
    """The renderer's **persistent palette state machine** — the resolved palette currently
    displayed *plus* any fade in progress. This is renderer-owned semantic state that keeps
    evolving while gameplay runs (e.g. an item-pickup fade): not a per-frame VGA side effect.

    ``colors`` is what is on screen now (16 resolved RGB). When ``phase != NONE`` a fade is
    running: every step moves each component of ``fade_from`` toward ``fade_to`` (the
    ``[0xACB7]`` target) by ``fade_amount`` (``pre2.recovered.transition.fade_palette``), so
    two consecutive snapshots show the fade advancing — and an enhanced renderer can smooth
    it on its own display clock instead of stepping per game tick. ``base_index`` selects the
    active **named** palette (``[0x2D8A]`` into the ``[0x2D00]`` table; swaps/cycles between
    named palettes are themselves visual state changes — see docs/pre2/render_model.md).
    """
    colors: tuple = ()           # 16 * (r,g,b) resolved DAC colours — what is displayed now
    base_index: int = 0          # [0x2D8A] selected named palette
    phase: "FadePhase" = FadePhase.NONE   # NONE / IN / OUT
    fade_amount: int = 0         # [0x6C03] progress (0..63)
    fade_from: bytes = b""       # 48-byte 6-bit source (the side being stepped) or empty
    fade_to: bytes = b""         # 48-byte 6-bit target ([0xACB7]) or empty


@dataclass(frozen=True)
class TransitionCmd:
    """A screen transition as render *state*, not raw palette/pixel writes."""
    kind: TransitionKind = TransitionKind.NONE
    center_x: int = 0     # IRIS: circle centre (the player), world/screen px
    center_y: int = 0
    radius: int = 0       # IRIS: current radius (shrinking)
    fade_amount: int = 0  # FADE: progress


@dataclass(frozen=True)
class AnimationState:
    """The renderer's **animated-tile cycle** — which of the ``frame_count`` remap frames is
    live now, plus the throttle pacing advances. A renderer-owned visual state machine that
    keeps evolving while gameplay runs (the background tiles cycle); recovered byte-exact in
    ``pre2.recovered.animation.advance_animation``. An enhanced renderer can blend frames on
    its own clock instead of stepping per redraw. ``active`` = animated tiles present this
    frame (``[0x6BBD]``); when false the cycle is frozen."""
    frame_index: int = 0       # which frame in the cycle (0..frame_count-1)
    frame_count: int = 3       # cycle length
    frame_ptr: int = 0         # raw [0x6BC2] remap-table offset (grounding back-reference)
    throttle_counter: int = 0  # [0x6BD4] per-frame counter
    throttle_period: int = 4   # frames between advances (4 normally, 2 when scrolling fast)
    active: bool = False       # [0x6BBD] animated tiles present this frame


@dataclass(frozen=True)
class CameraShakeState:
    """The screen-shake-on-fall as renderer-visible visual state (a persistent state machine that
    evolves while gameplay continues, like the palette fade). It is NOT hardware scrolling — the
    camera, fine-scroll, CRTC start and ``ega_display_start`` all stay put through the shake; the
    original perturbs the scene at render time.

    Recovered from memory: ``[0x6BEA]`` is the shake magnitude/timer, set to 7 (or 4) on a fall
    landing (by fall height) and decaying to 0 over ~37 frames (the 5A4A-5A6A group decay). The
    apply is CONFIRMED: 1030:4C30 overwrites the render row-stride factor ``[0x6BF8]`` (which is
    ``RendererState.row_factor`` and is already consumed by ``render_frame``) with the magnitude on
    odd frame parity / 0 on even — i.e. the gameplay viewport jolts vertically by ``{0, magnitude}``
    px, alternating with ``phase`` (confirmed by pixel cross-correlation; the HUD is untouched). So
    the FAITHFUL renderer already reproduces the shake byte-exact via ``row_factor``; an enhanced
    renderer can instead drive its own smooth shake from ``magnitude``/``phase``.
    """
    magnitude: int = 0      # [0x6BEA] shake amplitude/timer (0 = inactive)
    active: bool = False     # magnitude > 0
    phase: int = 0           # [0x6BD5] & 1 — frame parity the per-frame alternation rides on
    applied_offset: "int | None" = None   # [0x6BF8] vertical px offset this frame (0 when inactive);
                            # the row_factor render_frame already applies. See bridge._shake_state.


@dataclass(frozen=True)
class HudState:
    """The fixed-screen status-bar layer (score / lives / energy) as renderer-visible semantic
    state — it does not scroll with the world. Grounded in memory + drawn by the HUD render at
    1030:45B8 (it formats the score to ASCII ``[0x6F52]`` and blits digit/heart glyphs into the
    status bar). An enhanced renderer can lay the HUD out freely from these values.

    ``score`` is the DISPLAYED score (the engine keeps it ÷10 in ``[0x6C0E]`` and appends a fixed
    trailing 0, so display = internal*10). ``lives`` (``[0x27D8]``, the one-digit field is clamped
    to 9 when drawn) and ``energy`` (``[0x27D6]`` hearts) are the raw counts.
    """
    score: int = 0          # displayed score (= internal [0x6C0E]/[0x6C10] * 10)
    lives: int = 0          # [0x27D8]
    energy: int = 0         # [0x27D6] (hearts)


@dataclass(frozen=True)
class HudChromeAsset:
    """The static HUD chrome — loaded asset data (not rendered pixels). The status-bar background
    is a 320x23 planar bitmap (gray bar + baked LIVES:/SCORE:/ENERGY: labels + player-head icon +
    weapon-slot frame) at segment 0x252B:0x0B48; the glyph font (digits/hearts/weapon) is in the
    same chrome segment at 0x1610. Bridge-fed; the segment/offset knowledge stays in the bridge.

    The boss health meter is a separate boss-only sprite (id 0x135), not part of this chrome.
    """
    bar: bytes = b""    # 320x23 planar status-bar bitmap: 4 planes x 0x398 bytes (plane-major)
    font: bytes = b""   # HUD glyph-font segment bytes (blit_hud_glyph indexes 0x1610 + glyph*0x60)


@dataclass(frozen=True)
class GameFrameSnapshot:
    """One frame's complete render intent — the unit of verification and interpolation.

    Ordered exactly as the original draws (background tiles, then the sprite list in
    active-list order, then HUD/fixed-screen, with palette/transition as frame state).
    Two consecutive snapshots are everything an object-aware interpolator needs: matched
    ``base_id`` sprites + explicit camera/positions to lerp, animation carried from the
    newer frame.
    """
    camera: CameraState
    palette: PaletteState
    transition: TransitionCmd
    sprites: tuple = ()        # tuple[SpriteDrawCmd] in draw order (active-list order)
    tiles: tuple = ()          # tuple[TileDrawCmd] (empty until the tile lift; background
                               # is still rastered faithfully via the scroll ring)
    hud: tuple = ()            # tuple[SpriteDrawCmd] for fixed-screen elements (is_hud)
    phase: str = "gameplay"    # gameplay | intro | title | menu | map | loading | tally
    animation: "AnimationState" = AnimationState()   # animated-tile cycle state
    shake: "CameraShakeState" = CameraShakeState()   # camera-shake-on-fall visual state
    hud_state: "HudState" = HudState()               # status-bar values (score/lives/energy)
