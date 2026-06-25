"""EnhancedFrameState — the modern (RGB/RGBA) source-frame snapshot the enhanced compositor consumes.

Produced once per ~25 fps SOURCE frame by ``pre2.enhanced.extract`` (using the recovered/faithful planar
code only as an extractor), kept as prev+cur, and composited at the display refresh by
``pre2.enhanced.compositor`` — entirely in RGB/RGBA, no planar at display time. Grounded: every field is
derived from the byte-verified recovered state; nothing is invented and the VM framebuffer is never read.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class SpriteInstance:
    """One drawable sprite as a modern RGBA texture + its grounded screen placement.

    Cross-frame identity is ``handle`` — the object's persistent handle (the active-list record's pointer
    word at byte 6), which is stable across BOTH the walk/blink animation (unlike ``base_id = sprite_id &
    0x1FFF``, which changes every animation frame) AND active-list compaction on spawn (unlike the slot
    index, which shifts when objects are pushed). A handle can be REUSED after an object despawns, so the
    compositor also gates interpolation on a small per-frame world move. ``slot``/``base_id`` are kept for
    diagnostics only.

    Interpolation uses the **world** position ``world_x``/``world_y`` (the object's true location, smooth),
    NOT the screen position: the screen position folds in the per-animation-frame draw offset
    (``attr.x_off``/``y_off``, different for each walk frame), so interpolating it amplifies that ±1 jitter
    into visible shaking. ``screen_x``/``screen_y`` is the CURRENT frame's logical placement (= world − camera
    − offset); the compositor moves it by the world delta and keeps the current offset/camera fixed.
    ``tex_off_x``/``tex_off_y`` offset the cropped RGBA texture from ``screen`` (the tight bbox shifts with the
    animation frame). ``rgba`` is H×W×4 (alpha 0 = transparent), extracted bg-independently from the verified
    ``paint_sprite``. ``interpolate`` is False for fixed-screen HUD sprites. Draw order = order in ``sprites``."""
    handle: int
    slot: int
    base_id: int
    sprite_id: int
    world_x: int
    world_y: int
    screen_x: int
    screen_y: int
    tex_off_x: int
    tex_off_y: int
    rgba: np.ndarray
    interpolate: bool = True


@dataclass
class EnhancedFrameState:
    """A source frame projected into modern layers."""
    background_rgb: np.ndarray          # bg WITHOUT moving sprites (recovered render, object_camera=None)
    camera: tuple                       # (x_px, y_px) grounded source camera (for relative placement)
    sprites: list                       # SpriteInstance, in draw order
    faithful_rgb: np.ndarray            # the full faithful frame (fallback / alpha=1 parity oracle)
    unsupported: list = field(default_factory=list)   # [(base_id, mode_name)] sprites not interpolated (OPAQUE/ERASE)
    backdrop_rgb: "np.ndarray | None" = None   # the FIXED-screen parallax base layer (sky/mountains), rendered
                            # backdrop-only (all tiles forced type-1 restore_background). The scrolling tile
                            # layer is then `background_rgb != backdrop_rgb`; the compositor holds the backdrop
                            # still and scrolls only the tile layer (so the backdrop does NOT shake). None ->
                            # compositor falls back to a uniform whole-bg shift.
