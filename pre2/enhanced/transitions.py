"""Enhanced transition projections — render the recovered transition PHASE in RGB/native presentation space.

Each transition's geometry/phase is grounded in the recovered state (the same inputs the faithful planar
compositors consume); the enhanced renderer only projects them over the modern composed frame at higher
fidelity. It never invents timing -- the phase values (cleared-band extents, iris radius, curtain progress)
come straight from the captured VM state, so the transition follows the original cadence exactly.

Currently implemented: the VERTICAL fade-out (1030:30C6). Iris / curtain follow.
"""
from __future__ import annotations

import numpy as np

VIEWPORT_H = 176   # gameplay viewport rows; the vertical fade clears only these (the HUD band is separate)


def apply_vfade(frame, top_cleared: int, bot_start: int):
    """Project the recovered VERTICAL fade-out (1030:30C6) onto ``frame`` IN PLACE: black the two full-width
    bands converging from the top and bottom toward the middle -- rows ``[0, top_cleared)`` and
    ``[bot_start, VIEWPORT_H)`` -- exactly matching :func:`pre2.bridge.live_render.compose_vfade_planes`'s
    geometry. ``top_cleared``/``bot_start`` are the recovered phase (no interpolation -> original timing)."""
    t = max(0, min(VIEWPORT_H, int(top_cleared)))
    b = max(0, min(VIEWPORT_H, int(bot_start)))
    if t > 0:
        frame[:t] = 0
    if b < VIEWPORT_H:
        frame[b:VIEWPORT_H] = 0
    return frame


def apply_curtain(frame, new_frame, progress: float):
    """Project the center-out CURTAIN reveal of ``new_frame`` over a black ``frame``: a SMOOTH continuous band
    expanding symmetrically from the screen centre, driven by present-time ``progress`` 0..1 (0 = black, 1 =
    fully revealed). The smoother/modern projection of the recovered 1030:3054 panel_copy reveal -- same
    centre-out meaning, but a continuous pixel-granular edge instead of 16px strips (cf. the iris's smooth
    circle vs the EGA octant). Only the gameplay viewport rows are revealed; the HUD strip stays black."""
    p = 0.0 if progress < 0.0 else 1.0 if progress > 1.0 else progress
    w = frame.shape[1]
    half = int(round(p * (w / 2.0)))
    if half <= 0:
        return frame
    cx = w // 2
    frame[:VIEWPORT_H, max(0, cx - half):min(w, cx + half)] = \
        new_frame[:VIEWPORT_H, max(0, cx - half):min(w, cx + half)]
    return frame


def apply_iris(frame, radius: int, center_col: int, center_row: int):
    """Project the recovered end-level circular IRIS (1030:31F4) onto ``frame`` IN PLACE: keep everything
    inside the circle of ``radius`` about ``(center_col, center_row)`` visible, black outside. The original
    rasterises one octant of a cos/sin·radius circle and clears outside it; the enhanced projection draws a
    TRUE circle (smoother, anti-aliased 1px edge) with the SAME centre + radius -- a cleaner presentation of
    the same recovered phase, not a pixel-copy of the octant raster.

    NOTE on the centre: the recovered ``IrisState`` names its fields after the DGROUP offsets x_off/y_off
    (``[0x2DC6]``/``[0x2DC8]``), which are SWAPPED vs screen axes -- so the caller passes the screen COLUMN as
    ``center_col = iris.center_y`` and the screen ROW as ``center_row = iris.center_x`` (verified against the
    faithful clear: ~99% region agreement, the rest being the smooth-vs-octant edge). ``radius`` shrinks
    0xE6->0 over the transition (iris-out); ``radius<=0`` -> fully black."""
    h, w = frame.shape[:2]
    if radius <= 0:
        frame[:] = 0
        return frame
    yy = np.arange(h)[:, None]
    xx = np.arange(w)[None, :]
    dist = np.sqrt((xx - center_col) ** 2 + (yy - center_row) ** 2)
    alpha = np.clip(radius - dist + 0.5, 0.0, 1.0)         # 1 inside, 1px feather at the edge, 0 outside
    frame[:] = (frame.astype(np.float32) * alpha[..., None]).astype(np.uint8)
    return frame
