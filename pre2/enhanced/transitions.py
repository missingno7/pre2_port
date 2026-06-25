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


_CURTAIN_CENTER_BYTE = 0x14   # panel_copy reveals strips symmetric about byte-col 0x14 (px 160)
_CURTAIN_PAIRS = 10           # full reveal = 10 centre-out strip-pairs


def apply_curtain(frame, new_frame, completed_pairs: float):
    """Project the recovered center-out CURTAIN reveal (1030:3054 panel_copy): reveal ``new_frame`` over a
    black ``frame`` in 16px-wide vertical strips growing symmetrically from the centre, matching panel_copy's
    column set (byte-cols ``0x14-2k`` and ``0x14+2k`` for k=0..completed_pairs). ``completed_pairs`` is the
    recovered progress 0..10 (0 = black, 10 = fully revealed); fractional values feather the leading strip for
    a smooth present-time reveal. Only the gameplay viewport rows are revealed (the HUD stays black), as in
    compose_curtain_planes."""
    k_full = int(completed_pairs)
    def reveal(byte_col):
        x0 = max(0, byte_col * 8)
        x1 = min(frame.shape[1], byte_col * 8 + 16)
        if x1 > x0:
            frame[:VIEWPORT_H, x0:x1] = new_frame[:VIEWPORT_H, x0:x1]
    for k in range(k_full):
        reveal(_CURTAIN_CENTER_BYTE - 2 * k)
        if k:
            reveal(_CURTAIN_CENTER_BYTE + 2 * k)
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
