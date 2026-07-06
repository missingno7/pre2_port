"""SMOOTH-CAMERA (experimental enhancement): the presentation-camera X follow.

The model is pre2_editor's "vanilla" camera (the behaviour the user wants), which is a pixel-exact version
of the DOS camera: X is a CONTINUOUS BAND-DRAG — the camera moves the same frame by exactly the player's
overshoot past the [X1..X2] screen band, so walking *drags* the camera with the player (no parked-then-pan
threshold, no 2px scroll snap) — while Y keeps the full DOS centering state machine (native already computes
the byte-exact DOS cam_y; the presenter shows it sub-tick interpolated).

This module is pure math over the presentation camera; the DOS camera target (with all its per-level
centering / scripted modes) stays the byte-exact recovered one. The deviation clamp keeps the presentation
camera GLUED (within DEV px) to the DOS camera, so scripted pans / autoscroll / recentering still carry the
view along — the band-drag only reshapes the short-range follow feel."""
from __future__ import annotations

# The PRE2 player screen-x band (pre2_editor vanilla / blues TILEMAP_SCROLL_W*2): the camera holds still
# while the player is between X1 and X2 on screen, and is dragged by exactly the overshoot beyond either.
X1 = 128
X2 = 192
# Max deviation of the presentation camera from the DOS camera (px). Keeps scripted camera modes (autoscroll,
# recentering pans, boss cameras) glued: once the DOS camera walks away, the clamp drags the view along.
DEV = 48
# The extract/crop margin the presenter must reserve for the deviation (>= DEV + the max per-tick cam delta).
CROP = 64


def smooth_cam_x(scam_x: float, player_wx: float, dos_x: float, cur_x: float) -> float:
    """One presentation-camera X update: band-drag toward the player, clamped to the DOS camera's
    neighbourhood. ``scam_x`` = the current presentation camera, ``player_wx`` = the player's (interpolated)
    world x, ``dos_x`` = the (interpolated) DOS camera x, ``cur_x`` = the CURRENT tick's DOS camera x (the
    frame the compositor shifts from — bounds the shift to what the extract margin covers)."""
    x = float(scam_x)
    rel = player_wx - x
    if rel > X2:                       # player pushed past the right edge of the band -> dragged along
        x = player_wx - X2
    elif rel < X1:                     # past the left edge -> dragged along
        x = player_wx - X1
    x = max(dos_x - DEV, min(dos_x + DEV, x))    # stay glued to the DOS camera (scripted modes / recenter)
    x = max(cur_x - CROP, min(cur_x + CROP, x))  # never shift beyond the extracted margin
    return max(0.0, x)                           # world left edge
