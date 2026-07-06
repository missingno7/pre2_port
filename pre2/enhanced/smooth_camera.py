"""SMOOTH-CAMERA (experimental enhancement): the presentation-camera X follow.

Design (v4, from playtesting + measuring the recovered DOS camera):

* X = a RIGID, CENTERED BAND: the camera holds still while the player is inside the [X1..X2] band and is
  dragged 1:1 by the overshoot past either edge (pre2_editor-vanilla drag — inherently smooth because the
  player's presented position is sub-tick interpolated). The band straddles the display centre, so walking
  either direction rides the player near mid-screen (v3's off-centre band read as "the view is shifted").
* NO coupling to the DOS camera. v2/v3 clamped the presentation camera to the DOS camera's neighbourhood —
  but the measured DOS X camera is a park-then-PAN machine (pan-right until the player is 5 tiles from the
  camera, pan-left until 15, triggers at 16/4), so its placement swings the player across screen ~[8..271]
  and any tether eventually YANKS the smooth view at pan speed (the "instant shift"). With a centred band
  the deviation is STRUCTURALLY bounded (dos_rel-band_rel in ~[-180,+131]), so CROP=192 of extracted margin
  covers it and the hard clamp below is a pure safety that never engages in play.
* The world bounds are the DOS camera's own: left 0, right the [0x8164] tile limit (with the past-the-end
  0xEC backing-map rule) — at a wall both cameras pin identically (the player walks to the screen edge
  exactly like the faithful game).
* GLIDE on seed: the per-frame step is capped at (player speed + CATCHUP), dt-based — while band-dragging
  the cap always covers the player's own motion (zero lag), but an out-of-band seed (enabling mid-game,
  post-transition handoff) converges at CATCHUP instead of snapping.
* Y is not handled here: the presenter shows the DOS cam_y exactly (its per-level centering preserved),
  sub-tick interpolated.
"""
from __future__ import annotations

# The band in player-record units (player_world_x - camera_x). The record x is ~14px left of the drawn
# sprite and the sprite is ~24px wide, so the SPRITE CENTRE sits at screen ~(rel - 2): [140..188] rides the
# player around display centre (160) with 48px of drag slack.
X1 = 140
X2 = 188
# Extracted tile margin (px each side) the presenter reserves + crops; bounds |smooth - DOS| coverage.
# Structural max deviation with the centred band is ~180 (see module docstring) -> 192 never engages.
CROP = 192
# Seed/recovery glide speed (px/s) on top of the player's own speed.
CATCHUP = 240.0


def smooth_cam_x(scam_x: float, player_wx: float, player_vx: float, dt: float,
                 cur_x: float, world_max: float) -> float:
    """One presentation-camera X update (pure). ``player_vx`` px/s; ``dt`` s since the last update; ``cur_x``
    = the CURRENT tick's DOS camera x (the frame the compositor shifts from — extraction-coverage safety);
    ``world_max`` = the level's right camera limit in px (the DOS [0x8164] clamp)."""
    # rigid band target: unchanged inside [X1..X2], clamped into the band by the exact overshoot outside
    t = min(max(scam_x, player_wx - X2), player_wx - X1)
    t = max(0.0, min(world_max, t))                    # the DOS camera's own world bounds
    # glide cap: 1:1 during band-drag (the cap covers the player's own speed), CATCHUP-limited on seed
    lim = (abs(player_vx) + CATCHUP) * max(0.0, dt)
    d = t - scam_x
    if d > lim:
        d = lim
    elif d < -lim:
        d = -lim
    x = max(0.0, scam_x + d)
    return max(cur_x - CROP, min(cur_x + CROP, x))     # extraction-coverage safety (structurally inactive)


def world_max_px(w8164: int, player_wx: float) -> float:
    """The level's right camera limit in px — the DOS pan's own clamp [asm 3435: 344C-345E]: the header limit
    ``[0x8164]`` (tiles), or the full 256-tile backing map (0xEC + the 20-tile window) once the player is past
    the logical end (cave rooms beyond the main strip)."""
    lim = w8164 if w8164 < 0x8000 else 0                # signed guard (jge)
    if lim < (int(player_wx) >> 4) - 0x14:
        lim = 0xEC
    return float(lim << 4)
