"""Prehistorik 2 renderer — the consolidated, VM-independent frame entry.

This is the **replaceable-renderer seam**. ``render_frame`` composes the recovered
renderer leaves in the exact per-frame order the original runs them (validated in-VM on
gameplay snapshots 185902/212037 — see ``docs/pre2/renderer_status.md``):

    palette fade (6772) -> animated-grid redraw (3668) -> grid redraw (35A1)
        -> scroll copy (3A27)   [-> moving-sprite pass 26FA, layered by the caller]

``RendererState`` is the stable INPUT contract: a plain-data snapshot of everything the
renderer reads (tile map + attribute tables, camera/scroll bookkeeping, the current
animation-frame remap, and the palette-fade step). It is reconstructed from VM memory by
``pre2.bridge.render_state`` (read-only, one place); ``render_frame`` itself touches no
``cpu``/``mem``/``dos_re`` — so a future *native enhanced renderer* (frame interpolation,
higher fidelity, …) drops in by reimplementing ``render_frame`` against the same
``RendererState``.

Border (confirmed by profiling, see renderer_island.md): ``render_frame`` produces the
**renderer-owned** pixels — the scrolling tile background, the palette, and the
moving-sprite *list* pass. The **object system** (``65A0``/``8BFF`` iterating the
ObjectSlot data model) layers the gameplay sprites on top via the *shared* blit; it owns
gameplay state and is deliberately NOT part of the renderer.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.recovered.frame_renderer import (
    draw_grid, redraw_animated_grid, scroll_copy,
)
from pre2.recovered.object_render import paint_sprite, plan_frame
from pre2.recovered.transition import fade_palette

__all__ = ["RendererState", "FadeStep", "render_frame"]


@dataclass(frozen=True)
class FadeStep:
    """One palette-fade step's resolved inputs (direction already applied): step the
    6-bit palette ``a`` toward ``b`` by ``amount``. ``None`` when no fade is active."""
    a: bytes
    b: bytes
    amount: int


@dataclass(frozen=True)
class IrisState:
    """The circular-iris transition's per-frame state (the ``1030:31D0`` end-level loop):
    the shrinking ``radius`` and the circle centre (the player). ``None`` when no iris is
    running. Grounded in the verified iris recovery — ``radius``/``center_*`` are exactly the
    inputs ``pre2.bridge.transition.read_iris_inputs`` feeds ``build_scaled_columns``."""
    radius: int       # [0x2DD0] low byte, shrinks 0xE6 -> 0 over the transition
    center_x: int     # [0x2DC6] signed — circle centre X (player)
    center_y: int     # [0x2DC8] signed — circle centre Y (player)


@dataclass(frozen=True)
class RendererState:
    """Stable, VM-independent input contract for one rendered frame.

    Plain data only (no ``mem``); reconstructed by ``pre2.bridge.render_state``.
    """
    # --- tile map + per-tile attribute tables (all indexed by tile id) ---
    tiles: bytes            # level tile indices (row-major, stride 0x100)
    type_tbl: bytes         # 1A0F:0x805E — OR'd into [0x2DF2] over the grid
    flag_tbl: bytes         # 1A0F:0x6988 — animated-tile draw flag
    blit_type: bytes        # 1A0F:0x4DF8 — sprite/tile transparency class
    mask_region: bytes      # 1A0F:0x2DF8 — type>=2 transparency masks
    anim_xlat: bytes        # current animation frame remap ([[0x6BC2] .. +256])
    # --- camera / scroll bookkeeping ---
    camera_x: int           # [0x2DE4] tiles
    camera_y: int           # [0x2DE6] tiles
    prev_x: int             # [0x2DE0] (dirty compare; 0x55AA sentinel forces redraw)
    prev_y: int             # [0x2DE2]
    col_ring: int           # [0x2DE8] column ring index / animated-grid fine column
    fine_scroll: int        # [0x6BC4] sub-tile pixel scroll
    row_ring: int           # [0x2DEA] row ring index
    scroll_src: int         # [0x2DBA] source offset into the ring buffer
    dest_page: int          # [0x2DD8] destination page (back buffer)
    row_factor: int         # [0x6BF8] scroll row-stride factor
    dirty: int              # [0x2DF4]
    dirty_rows: int         # [0x2DF5]
    fade: FadeStep | None   # palette fade step, or None when inactive
    palette: "PaletteState | None" = None  # full palette state machine (render_model.PaletteState:
                            # displayed colours + base_index + IN/OUT phase), or None if not captured
    iris: "IrisState | None" = None  # circular-iris transition state, or None when no iris ([0x2DD0]==0)
    anim: "AnimStep | None" = None    # animated-tile cycle inputs (pre2.recovered.animation.AnimStep)
    shake: "CameraShakeState | None" = None  # camera-shake-on-fall state (render_model.CameraShakeState)
    # --- moving-sprite pass (26FA); object_camera None => skip it ---
    object_camera: object = None     # object_render.Camera (frame counter post-incremented)
    object_sprites: tuple = ()       # the active-sprite list (object_render.Sprite records)
    object_attrs: dict | None = None  # sprite_id -> object_render.SpriteAttr
    object_src_banks: dict | None = None  # src_seg -> 64 KiB sprite-pixel segment bytes


def render_frame(state: RendererState, planes, dac=None):
    """Render one frame's **renderer-owned** output into ``planes`` (and ``dac``).

    ``planes`` is the four EGA plane buffers (the scrolling ring buffer + the visible
    pages + the sprite cache all live within them, exactly as in VRAM). ``dac``, if
    given, is a 16-entry list of ``[r, g, b]`` 6-bit DAC colours updated by the fade.

    Composes the recovered leaves in the original per-frame order. Returns the
    :class:`~pre2.recovered.frame_renderer.GridResult` from the grid redraw (its dirty /
    prev-camera contract), so the caller can persist it if running statefully.

    The moving-sprite pass (1030:26FA ``object_render``) and the object system are layered
    by the caller on top of this result (see the module docstring's border note).
    """
    s = state

    # 1) palette fade — 6772 (DAC only; no plane effect)
    if s.fade is not None and dac is not None:
        out, _done = fade_palette(s.fade.a, s.fade.b, s.fade.amount)
        for i in range(16):
            dac[i] = [out[3 * i] & 0x3F, out[3 * i + 1] & 0x3F, out[3 * i + 2] & 0x3F]

    # 2) animated-grid redraw — 3668 (redraw the animated background tiles into the ring)
    redraw_animated_grid(
        planes, s.tiles, s.type_tbl, s.flag_tbl, s.anim_xlat, s.blit_type,
        s.camera_x & 0xFF, s.camera_y & 0xFF, s.col_ring, s.scroll_src,
    )

    # 3) grid redraw — 35A1 (full visible-grid redraw; early-exits unless the camera moved)
    grid = draw_grid(
        planes, _TileMapView(s), s.camera_x, s.camera_y, s.prev_x, s.prev_y,
        s.dirty, s.dirty_rows, s.scroll_src, s.col_ring, s.fine_scroll,
        s.blit_type, s.mask_region,
    )

    # 4) scroll copy — 3A27 (blit the ring buffer to the visible page)
    scroll_copy(
        planes, s.scroll_src, s.dest_page, s.col_ring, s.fine_scroll,
        s.row_ring, s.row_factor,
    )

    # 5) moving-sprite pass — 26FA (the active-sprite list; cull/animate/position/clip
    #    + planar blit). Layered on top of the scrolled background. Record mutations
    #    (life/drawn) are the object-record write-back contract, not part of the pixels.
    if s.object_camera is not None:
        banks = s.object_src_banks or {}
        for draw in plan_frame(s.object_sprites, s.object_attrs or {}, s.object_camera):
            bank = banks.get(draw.src_seg, b"")
            size = draw.src_bw * draw.full_rows * 6 + 64   # [asm read_source extent]
            paint_sprite(planes, draw, bank[draw.src_off:draw.src_off + size],
                         s.object_camera.row_stride)

    return grid


class _TileMapView:
    """Adapt ``RendererState`` to the ``.tiles``/``.tile_flags``/``.tile_type`` shape
    :func:`draw_grid` expects (it reads tile indices + the two attribute tables it ORs
    and dispatches on). Keeps ``RendererState`` flat while reusing the verified draw."""

    __slots__ = ("tiles", "tile_flags", "tile_type")

    def __init__(self, s: RendererState):
        self.tiles = s.tiles
        self.tile_flags = s.type_tbl   # draw_grid ORs this into [0x2DF2]
        self.tile_type = s.blit_type   # draw_grid dispatches the blit on this
