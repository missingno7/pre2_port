"""Enhanced presentation v1: inter-frame scroll-motion interpolation of faithful RGB frames.

The faithful VM produces one correct frame per game tick (``render_planar_rgb`` of the EGA
page). Between ticks the camera moves by a few pixels; this layer draws intermediate frames by
shifting the gameplay viewport toward the previous camera position, so a 70 Hz display gets
smooth sub-tick scrolling instead of the game's lower tick rate. The HUD band stays fixed (it
does not scroll). Pure: numpy in, numpy out — no cpu/mem.

This is v1 (camera/scroll motion). Per-object sprite interpolation (drawing each moving sprite
at its interpolated position from the GameFrameSnapshot) is the v2 layer on top of this.
"""
from __future__ import annotations

import numpy as np

DEFAULT_HUD_TOP = 184   # gameplay viewport = rows [0, 184); status bar = rows [184, 200)


def shift_viewport(rgb: np.ndarray, dx: int, dy: int, hud_top: int = DEFAULT_HUD_TOP) -> np.ndarray:
    """Return ``rgb`` with the gameplay viewport (rows ``[0, hud_top)``) translated by ``(dx, dy)``
    pixels; the HUD band (rows ``[hud_top, H)``) is untouched. Exposed edges replicate the border
    row/column (no wrap-around)."""
    out = rgb.copy()
    if dx == 0 and dy == 0:
        return out
    game = rgb[:hud_top]
    shifted = np.roll(game, (dy, dx), axis=(0, 1))
    # replicate the edge into the strip np.roll wrapped in, so the border doesn't show the
    # opposite edge during a scroll
    if dy > 0:
        shifted[:dy] = game[0]
    elif dy < 0:
        shifted[dy:] = game[-1]
    if dx > 0:
        shifted[:, :dx] = shifted[:, dx:dx + 1]
    elif dx < 0:
        shifted[:, dx:] = shifted[:, dx - 1:dx]
    out[:hud_top] = shifted
    return out


def scroll_subframes(prev_cam, cur_cam, cur_rgb, steps: int,
                     hud_top: int = DEFAULT_HUD_TOP) -> list:
    """``steps`` interpolated frames for the interval *(prev, cur]*: frame ``k`` (k=1..steps)
    shows ``cur_rgb`` shifted so the viewport sits at the interpolated camera between ``prev_cam``
    and ``cur_cam`` (both ``(x_px, y_px)``). The last frame (k=steps) is ``cur_rgb`` unshifted.

    Image shift is opposite the camera motion: at fraction ``f`` toward cur, the viewport is offset
    by ``(prev-cur)*(1-f)`` px (so f=0 -> previous position, f=1 -> cur)."""
    dxn = cur_cam[0] - prev_cam[0]
    dyn = cur_cam[1] - prev_cam[1]
    frames = []
    for k in range(1, steps + 1):
        f = k / steps
        dx = round(-dxn * (1.0 - f))
        dy = round(-dyn * (1.0 - f))
        frames.append(shift_viewport(cur_rgb, dx, dy, hud_top))
    return frames
