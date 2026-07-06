"""SMOOTH-CAMERA (experimental enhancement): the presentation-camera X follow.

The model is pre2_editor's "vanilla" camera (the behaviour the user wants): X is a CONTINUOUS BAND-DRAG —
the camera holds still while the player is inside the [X1..X2] screen band and is dragged along by the
player's overshoot past either edge — while Y keeps the full DOS centering state machine (native already
computes the byte-exact DOS cam_y; the presenter shows it sub-tick interpolated).

Two hard-won rules (v2 -> v3, from playtesting):
  * NO gluing to the DOS camera: the measured DOS X camera is a park-then-PAN machine that lets the player
    reach screen ~230 before panning and OVERSHOOTS ahead of the walk direction on recenter — clamping the
    band camera to its neighbourhood re-injects exactly the jumpy motion the enhancement removes. Instead
    the band camera clamps to the REAL world bounds (the same [0x8164] tile limit the DOS pan uses), plus
    the extract-coverage bound.
  * NEVER jump — GLIDE: the camera's per-frame step is rate-limited to (player speed + CREEP). At the band
    edge the drag follows the player 1:1 (the limit always covers the player's own motion); when the camera
    finds itself far outside the band (enable-time seed, DOS handoff), it glides back at CREEP instead of
    snapping. Frame-rate independent (dt-based)."""
from __future__ import annotations

# The player screen band (world-relative: player_world_x - camera_x; the sprite draw offset is ~14px, so
# these correspond to screen ~[114..178] — pre2_editor vanilla feel). Camera holds inside, drags outside.
X1 = 128
X2 = 192
# Extract-coverage bound (px each side): the presenter extracts this much extra tile margin and crops it
# back off, so the band camera may deviate from the DOS camera by up to CROP and still show real tiles.
# (Measured worst DOS pan-overshoot deviation is ~94px away from world edges; 128 leaves headroom so the
# coverage clamp never engages in normal play — engaging it steps the camera visibly.)
CROP = 128
# Recenter glide speed (px/s) when the camera is outside the band but the player isn't pushing it.
CREEP = 45.0


def smooth_cam_x(scam_x: float, player_wx: float, player_vx: float, dt: float,
                 cur_x: float, world_max: float) -> float:
    """One presentation-camera X update (pure). ``player_vx`` in px/s; ``dt`` seconds since the last update;
    ``cur_x`` = the CURRENT tick's DOS camera x (the frame the compositor shifts from — bounds the shift to
    the extracted margin); ``world_max`` = the level's right camera limit in px (the DOS [0x8164] clamp)."""
    # the band target: hold inside [X1..X2], dragged by the exact overshoot outside
    t = scam_x
    rel = player_wx - scam_x
    if rel > X2:
        t = player_wx - X2
    elif rel < X1:
        t = player_wx - X1
    t = max(0.0, min(world_max, t))                    # the DOS camera's own world bounds
    t = max(cur_x - CROP, min(cur_x + CROP, t))        # never target beyond the extracted margin
    # glide, never jump: per-frame step capped at player speed + CREEP (locks 1:1 at the band edge while
    # walking; gently recenters from an out-of-band seed instead of snapping)
    lim = (abs(player_vx) + CREEP) * max(0.0, dt)
    d = t - scam_x
    if d > lim:
        d = lim
    elif d < -lim:
        d = -lim
    x = max(0.0, scam_x + d)
    return max(cur_x - CROP, min(cur_x + CROP, x))     # hard coverage clamp (holes otherwise)


def world_max_px(w8164: int, player_wx: float) -> float:
    """The level's right camera limit in px — the DOS pan's own clamp [asm 3435: 344C-345E]: the header limit
    ``[0x8164]`` (tiles), or the full 256-tile backing map (0xEC + the 20-tile window) once the player is past
    the logical end (cave rooms beyond the main strip)."""
    lim = w8164 if w8164 < 0x8000 else 0                # signed guard (jge)
    if lim < (int(player_wx) >> 4) - 0x14:
        lim = 0xEC
    return float(lim << 4)
