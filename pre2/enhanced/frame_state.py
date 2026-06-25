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

    Cross-frame identity is ``slot`` — the object's ACTIVE-LIST RECORD INDEX, which is stable across the
    walk/blink animation (unlike ``base_id = sprite_id & 0x1FFF``, which changes every animation frame and so
    must NOT be used to match objects). ``screen_x``/``screen_y`` are the sprite's logical top-left placement
    (the interpolation anchor); ``tex_off_x``/``tex_off_y`` offset the cropped RGBA texture from that anchor
    (so the texture, whose tight bbox shifts with the animation frame, is drawn at ``screen + tex_off``).
    ``rgba`` is H×W×4 (alpha 0 = transparent), extracted bg-independently from the verified ``paint_sprite``.
    ``interpolate`` is False for fixed-screen HUD/boss-meter sprites. Draw order = position in ``sprites``."""
    slot: int
    base_id: int
    sprite_id: int
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
