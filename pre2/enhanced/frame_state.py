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
    """One drawable sprite as a modern RGBA texture + its grounded screen anchor.

    ``rgba`` is H×W×4 (alpha 0 = transparent), extracted bg-independently from the verified ``paint_sprite``.
    ``anchor_x``/``anchor_y`` are the texture's top-left on screen (px). ``base_id`` is the cross-frame
    identity for interpolation; ``interpolate`` is False for fixed-screen HUD/boss-meter sprites (drawn at
    their anchor, not lerped). Draw order = position in the owning frame's ``sprites`` list."""
    base_id: int
    sprite_id: int
    anchor_x: int
    anchor_y: int
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
