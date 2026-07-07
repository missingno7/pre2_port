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
* Y = the SAME rigid centered band, vertical (:func:`smooth_cam_y`) — fully decoupled from the DOS camera,
  exactly like X. The DOS vertical follow (1030:5663) is a dead-zone + re-center machine (park while the
  player drifts across the zone, then scroll ~128px to re-center): following it — raw (v4), eased (the deleted
  "bumping strength" dial), spring (v2) or curve-law (v3) — always kept those discrete re-center bands.
  The band drag removes them: the camera moves ONLY when the player pushes past the band edge, 1:1 and
  sub-tick-interpolated, on a platform ride it just rides along. The LEVEL's framing signals are processed the
  modern way: the airborne LOOK-AHEAD (DOS latches target row 3 falling / 8 rising = a 96px pan) becomes a
  smooth velocity-driven band bias (LOOK_K/LOOK_RATE); the ground re-center targets are deliberately DROPPED
  (they only exist because the DOS dead-zone lets the player drift 4+ rows off-centre — the band already holds
  mid-screen); the hard limits (bottom [0x2CF5], room locks [0x6BD9]/[0x8166]&2) are respected via clamps /
  room_mode; a forced-auto-scroll level ([0x8166]&4) falls back to following the interpolated DOS cam (the
  camera moves regardless of the player there — a band would fight it). Deviation from the DOS cam_y is
  STRUCTURALLY bounded (~152px, both cameras frame the same player) < Y_V_PAD: TILES covered by the extract's
  vertical over-extraction (tile_ext), OBJECTS by the vertical re-admission (the v_pad window in extract's
  _replan_wide — the game computes objects only for ITS OWN viewport, so the presentation re-plans them).
  Shake-free: the [0x6BF8] landing jolt is removed from the clamp reference and the tile slice together.
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
                 cur_x: float, world_max: float, m_disp: float = 0.0, crop: float = CROP) -> float:
    """One presentation-camera X update (pure). ``player_vx`` px/s; ``dt`` s since the last update; ``cur_x``
    = the CURRENT tick's DOS camera x (the frame the compositor shifts from — extraction-coverage safety);
    ``world_max`` = the level's right camera limit in px (the DOS [0x8164] clamp); ``m_disp`` = the DISPLAYED
    widescreen margin px/side (0 = no widescreen); ``crop`` = the extracted over-coverage px/side.

    RESPECT THE LEVEL EDGE (like the regular true-widescreen margin slide): the DISPLAYED window is
    ``[x - m_disp, x + 320 + m_disp]``, so keeping the WHOLE window inside the world (no beyond-the-world black
    void at the ends) means clamping x to ``[m_disp, world_max - m_disp]`` — one margin narrower each side than
    the DOS camera's own ``[0, world_max]``. Pinning there costs up to ``m_disp`` of deviation from ``cur_x``,
    so the caller sizes ``crop = max(CROP, m_disp)`` to keep that shift covered.

    (A FIXED-CAMERA cave — the game's [0x6BD9]/[0x8166]&2 gate — is handled at the call site by NOT smoothing
    at all there: the faithful fixed camera is presented and the widescreen margins are blacked (room_mode).)"""
    lo, hi = m_disp, world_max - m_disp
    if lo > hi:                                        # world narrower than the display -> centre it (edges void)
        lo = hi = 0.5 * world_max
    # rigid band target: unchanged inside [X1..X2], clamped into the band by the exact overshoot outside
    t = min(max(scam_x, player_wx - X2), player_wx - X1)
    t = max(lo, min(hi, t))                            # the level's world bounds, inset by the display margin
    # glide cap: 1:1 during band-drag (the cap covers the player's own speed), CATCHUP-limited on seed
    lim = (abs(player_vx) + CATCHUP) * max(0.0, dt)
    d = t - scam_x
    if d > lim:
        d = lim
    elif d < -lim:
        d = -lim
    x = max(lo, min(hi, scam_x + d))
    return max(cur_x - crop, min(cur_x + crop, x))     # extraction-coverage safety


def world_max_px(w8164: int, player_wx: float) -> float:
    """The level's right camera limit in px — the DOS pan's own clamp [asm 3435: 344C-345E]: the header limit
    ``[0x8164]`` (tiles), or the full 256-tile backing map (0xEC + the 20-tile window) once the player is past
    the logical end (cave rooms beyond the main strip)."""
    lim = w8164 if w8164 < 0x8000 else 0                # signed guard (jge)
    if lim < (int(player_wx) >> 4) - 0x14:
        lim = 0xEC
    return float(lim << 4)


# --- Y: a rigid centered BAND dragged by the player — the X design, decoupled from the DOS camera -------------
#
# v5, from playtesting v4 (the DOS-follow + optional lag): the DOS vertical follow (1030:5663) is a dead-zone +
# re-center machine — riding a platform (native_snap 205141) the camera PARKS for ~16 ticks while the player
# drifts ~4 screen rows, then RACES ~128px in ~9 ticks to re-center, then parks again. Following it (however
# smoothly eased) reproduces exactly those "discrete bands"; the user wants them GONE, like X. So Y now mirrors
# smooth_cam_x: a rigid band straddling the display centre, dragged 1:1 by the (sub-tick interpolated) player
# past its edges, DOS camera ignored. NO added easing on the drag: the softened-follow "bumping strength" dial
# (v4) was deleted with the follow — a band drag has no re-center scrolls left to soften, and any lag on the
# drag grows the deviation bound (the v_pad/object-re-admission budget) for no felt benefit.
#
# The deviation from the DOS camera is STRUCTURALLY bounded: both cameras frame the same player, DOS keeps its
# baseline on ITS screen in (0..176+sprite] (hard: the planner culls outside), the band keeps it in
# [Y1..Y2] = [72..104] — so |dev| <= max(176-Y1, Y2-0) ~ 104 < Y_V_PAD-1. Coverage: TILES come from the
# extract's vertical over-extraction (tile_ext); OBJECTS from the vertical re-admission (extract's
# _replan_wide v_pad window — the game itself computes objects only for ITS viewport).
Y_V_PAD = 160            # vertical over-extraction + re-admission px/side; >= the structural bound: player
#                          presented screen-y in [_Y1-LOOK_DOWN, _Y2+LOOK_UP]=[24,128], DOS baseline in (0,176]
#                          -> |dev| <= 152 < 159.
_Y1 = 72.0               # the band in player-BASELINE units (record [0x4F1E] == sprite world_y == cmd.world_y):
_Y2 = 104.0              # baseline screen-y rides [72..104], straddling the 176-row viewport centre (88) with
#                          32px of drag slack — the player sits near mid-screen, like X's [140..188] around 160.
# LOOK-AHEAD: the modern form of the LEVEL's airborne framing signal (the DOS 5663 look-ahead — falling latches
# target row 3 == a 96px pan; rising row 8). Instead of a latched pan, a velocity-driven BIAS shifts the band:
# b = pvy * LOOK_K clamped to [-LOOK_UP, +LOOK_DOWN], slew-limited by LOOK_RATE. Positive b slides the band UP
# the screen (player presented higher -> more world visible below, where you're falling). Because the band is a
# CLAMP, a moving bias never pushes the camera by itself — it only changes which edge the player rides and
# where that edge sits, so engage/release are inherently smooth (no re-center snap, no latch).
LOOK_K = 0.12            # s — bias per px/s of player fall speed (terminal ~373px/s -> ~45px, near LOOK_DOWN)
LOOK_DOWN = 48.0         # px max look-down (falling; DOS pans to row 3 = 40px more than neutral — comparable)
LOOK_UP = 24.0           # px max look-up (rising; gentler — jumps are brief)
LOOK_RATE = 140.0        # px/s bias slew (well under CATCHUP so the glide cap always covers bias motion)


def smooth_cam_y(sy: float, look: float, pwy: float, pvy: float, dt: float, cam_uninterp: float,
                 bottom_px: float, dos_follow: float | None = None,
                 v_pad: float = Y_V_PAD) -> tuple[float, float]:
    """One presentation-camera Y update (pure): the X band-drag, vertical, + velocity look-ahead bias.
    Returns ``(new_sy, new_look)``.

    ``sy``/``look`` = the presentation cam_y and the current look-ahead bias (state); ``pwy`` = the player's
    sub-tick interpolated world-y BASELINE (the record [0x4F1E]); ``pvy`` px/s (+ = falling); ``dt`` s since
    the last update; ``cam_uninterp`` = the CURRENT tick's shake-free DOS cam_y (``cam_y*16 + fine``, no
    [0x6BF8] jolt — the compositor slices the tile layer at ``cam_uninterp - present_cam_y``, so the
    extraction-coverage clamp is taken against it); ``bottom_px`` = the DOS bottom camera limit
    ([0x2CF5]-0xB tiles, px).

    ``dos_follow``: the FORCED-SCROLL escape — on a level whose camera header sets [0x8166]&4 the DOS camera
    auto-scrolls regardless of the player, and a band would fight it into the coverage clamp (stair-stepping);
    pass the interpolated shake-free DOS cam_y to follow it directly instead (smooth: it is interpolated).

    Rigid band target (identical shape to :func:`smooth_cam_x`): unchanged while the player's presented
    baseline sits inside the (bias-shifted) band, dragged by the exact overshoot outside — inherently smooth
    because the player position is sub-tick interpolated. GLIDE cap on seed/recovery; the DOS camera's own
    world bounds [0, bottom]; the coverage clamp is a pure safety (structural bound ~152 < v_pad-1)."""
    if dos_follow is not None:                         # forced auto-scroll level: follow the DOS cam directly
        sy, look = float(dos_follow), 0.0
        sy = min(max(sy, 0.0), max(0.0, bottom_px))
        lim = max(0.0, v_pad - 1.0)
        return min(max(sy, cam_uninterp - lim), cam_uninterp + lim), look
    want = min(max(pvy * LOOK_K, -LOOK_UP), LOOK_DOWN)  # the level's airborne framing signal, velocity form
    db = want - look                                    # slew-limited engage/release (framerate-independent)
    lim = LOOK_RATE * max(0.0, dt)
    look += min(max(db, -lim), lim)
    # rigid band, shifted by the bias: player screen-y rides [_Y1 - look, _Y2 - look] (falling: higher on
    # screen -> more visible below). A clamp, so bias motion alone never moves the camera.
    t = min(max(sy, pwy - _Y2 + look), pwy - _Y1 + look)
    t = min(max(t, 0.0), max(0.0, bottom_px))           # the DOS camera's own world bounds
    lim = (abs(pvy) + CATCHUP) * max(0.0, dt)           # glide: 1:1 during drag (covers bias<=LOOK_RATE too)
    d = t - sy
    if d > lim:
        d = lim
    elif d < -lim:
        d = -lim
    sy = min(max(sy + d, 0.0), max(0.0, bottom_px))
    lim = max(0.0, v_pad - 1.0)                         # extraction/re-admission coverage safety
    return min(max(sy, cam_uninterp - lim), cam_uninterp + lim), look
