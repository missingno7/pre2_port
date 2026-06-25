"""Enhanced transition projections — render the recovered transition PHASE in RGB/native presentation space.

Each transition's geometry/phase is grounded in the recovered state (the same inputs the faithful planar
compositors consume); the enhanced renderer only projects them over the modern composed frame at higher
fidelity. It never invents timing -- the phase values (cleared-band extents, iris radius, curtain progress)
come straight from the captured VM state, so the transition follows the original cadence exactly.

Currently implemented: the VERTICAL fade-out (1030:30C6). Iris / curtain follow.
"""
from __future__ import annotations

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
