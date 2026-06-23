"""Assemble a :class:`GameFrameSnapshot` (semantic render intent) from ``RendererState``.

    RendererState (read from VM by pre2.bridge.render_state)
      -> build_frame_snapshot
      -> GameFrameSnapshot (render commands: tiles, sprites, camera, palette, transition)

Pure: no ``cpu``/``mem``/``planes``. This is the render-intent counterpart of the faithful
rasteriser (``render_frame``): the same inputs, but emitted as *what to draw* rather than
*planar bytes*. The background tile enumeration (:func:`plan_tiles`) replaces the scroll-ring
machinery with the plain "tile T at grid (c,r) -> screen (x,y)" mapping; the sprite list
reuses :func:`object_render.plan_sprite_command`; palette/transition are exposed as state.
"""
from __future__ import annotations

from pre2.recovered.animation import frame_index as _anim_frame_index
from pre2.recovered.animation import throttle_period as _anim_throttle_period
from pre2.recovered.animation import FRAME_COUNT as _ANIM_FRAME_COUNT
from pre2.recovered.object_render import plan_sprite_command
from pre2.recovered.render_model import (
    TILE_PX, AnimationState, CameraShakeState, CameraState, GameFrameSnapshot, PaletteState,
    TileDrawCmd, TransitionCmd, TransitionKind,
)

TILEMAP_STRIDE = 0x100   # tile map is row-major, 0x100 stride [render_frame: tiles]
VISIBLE_COLS = 0x14      # 20 tiles across [frame_renderer.VISIBLE_COLS]
VISIBLE_ROWS = 0x0C      # 12 tile rows    [frame_renderer.VISIBLE_ROWS]


def plan_tiles(state) -> tuple:
    """Enumerate the visible 20x12 background grid as semantic :class:`TileDrawCmd`s.

    Walks ``tiles[(camera_y+row)*0x100 + camera_x + col]`` (the same indexing the faithful
    ``draw_tile_row``/``draw_grid`` use: ``si = camera_y*0x100 + camera_x``, +1 per column,
    +0x100 per row) and places each tile at its grid-relative screen position (16 px/tile).
    The sub-tile camera offset lives in :class:`CameraState` (applied to the whole grid by
    the renderer), not per tile.
    """
    tiles = state.tiles
    blit_type = state.blit_type
    base = (state.camera_y * TILEMAP_STRIDE + state.camera_x) & 0xFFFF
    out = []
    for row in range(VISIBLE_ROWS):
        si = (base + row * TILEMAP_STRIDE) & 0xFFFF
        for col in range(VISIBLE_COLS):
            idx = (si + col) & 0xFFFF
            tile_id = tiles[idx] if idx < len(tiles) else 0
            type_attr = blit_type[tile_id] if tile_id < len(blit_type) else 0
            out.append(TileDrawCmd(tile_id=tile_id, grid_col=col, grid_row=row,
                                   screen_x=col * TILE_PX, screen_y=row * TILE_PX,
                                   type_attr=type_attr))
    return tuple(out)


def plan_sprite_commands(state):
    """The active-sprite list as semantic :class:`SpriteDrawCmd`s, in draw order, split
    into world sprites and fixed-screen HUD (mirrors ``plan_frame``'s walk + attr lookup)."""
    if state.object_camera is None:
        return (), ()
    attrs = state.object_attrs or {}
    sprites, hud = [], []
    for spr in state.object_sprites:
        if spr.sprite_id == 0xFFFF:
            continue
        attr = attrs.get(spr.sprite_id)
        if attr is None:
            continue
        cmd = plan_sprite_command(spr, attr, state.object_camera)
        if cmd is None:
            continue
        (hud if cmd.is_hud else sprites).append(cmd)
    return tuple(sprites), tuple(hud)


def _palette(state) -> PaletteState:
    # Prefer the full palette state machine (displayed colours + IN/OUT phase + base index),
    # captured when the bridge had `dos`. Fall back to the fade-only step otherwise.
    p = getattr(state, "palette", None)
    if p is not None:
        return p
    f = state.fade
    if f is None:
        return PaletteState()
    return PaletteState(fade_from=f.a, fade_to=f.b, fade_amount=f.amount)


def _animation(state) -> AnimationState:
    """The animated-tile cycle as render state (which frame is live + the throttle pacing)."""
    a = getattr(state, "anim", None)
    if a is None:
        return AnimationState()
    return AnimationState(
        frame_index=_anim_frame_index(a.frame_ptr), frame_count=_ANIM_FRAME_COUNT,
        frame_ptr=a.frame_ptr, throttle_counter=a.throttle,
        throttle_period=_anim_throttle_period(a.speed), active=a.active,
    )


def _shake(state) -> CameraShakeState:
    """The camera-shake-on-fall visual state (named state machine), or the inactive default."""
    return getattr(state, "shake", None) or CameraShakeState()


def _transition(state) -> TransitionCmd:
    """The active screen transition as render state. Today: the circular IRIS (centre +
    shrinking radius); ``None`` iris -> NONE. The palette FADE is carried in PaletteState
    (a fade can overlap gameplay), so it stays there; this is the geometric transition."""
    iris = getattr(state, "iris", None)
    if iris is None:
        return TransitionCmd()
    return TransitionCmd(kind=TransitionKind.IRIS, center_x=iris.center_x,
                         center_y=iris.center_y, radius=iris.radius)


def build_frame_snapshot(state) -> GameFrameSnapshot:
    """Assemble one frame's full render intent from ``RendererState``.

    Camera (pixel-precise), palette (resolved/fade), transition state, the ordered sprite
    list, the HUD list, and the tile background — everything two consecutive snapshots need
    for object-aware interpolation. The faithful renderer still rasters this byte-exact; the
    enhanced renderer will consume it directly, on its own clock.
    """
    cam = CameraState(
        x_px=state.camera_x * TILE_PX,
        y_px=state.camera_y * TILE_PX + state.fine_scroll,
        cam_tile_x=state.camera_x, cam_tile_y=state.camera_y,
        fine_scroll=state.fine_scroll,
    )
    sprites, hud = plan_sprite_commands(state)
    return GameFrameSnapshot(
        camera=cam, palette=_palette(state), transition=_transition(state),
        sprites=sprites, tiles=plan_tiles(state), hud=hud, phase="gameplay",
        animation=_animation(state), shake=_shake(state),
    )
