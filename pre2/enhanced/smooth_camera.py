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

from math import exp as _exp

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
# baseline on ITS screen in (0..176+sprite] (hard: the planner culls outside), the band keeps it near centre —
# so |dev| stays well under Y_V_PAD-1. Coverage: TILES come from the extract's vertical over-extraction
# (tile_ext); OBJECTS from the vertical re-admission (extract's _replan_wide v_pad window — the game itself
# computes objects only for ITS viewport).
Y_V_PAD = 112            # vertical over-extraction + re-admission px/side; covers the structural band bound +
#                          the look-ahead bias + the SMOOTHING lag. Measured worst |sy - DOS cam_y| across the
#                          captured caves/falls = 63px rigid / 76px at HIGH; 112 gives ~35px headroom over that
#                          while keeping the extract's tile-row count (per-tick cost) down — 176 was ~2.4x the
#                          real need and tripled the vertical tile render for no coverage benefit.
# The dead-zone band is defined on the player's BODY CENTRE, not the feet BASELINE the record ([0x4F1E]) gives:
# the sprite is ~36px tall drawn UP from the baseline, so body-centre == baseline - _FEET_OFFSET. Framing the
# baseline at the viewport centre (the old [72..104]) put the visible BODY ~18px high -> the player rode the top
# half. Centre the BODY at the viewport middle (88) instead, and widen the zone to a real dead-zone.
_FEET_OFFSET = 18.0      # feet baseline -> body centre (~half the 36px standing sprite; fixed, so the camera
#                          does not bob as the sprite's height animates — the feet are the stable anchor)
_BODY_CENTRE = 88.0      # target body screen-y == the 176-row viewport middle (symmetric framing)
_BAND_HW = 30.0          # dead-zone half-height (60px zone, was a slim 32) — the player drifts freely within it
_Y1 = _BODY_CENTRE + _FEET_OFFSET - _BAND_HW   # feet ride [76..136] -> BODY rides [58..118], centred on 88
_Y2 = _BODY_CENTRE + _FEET_OFFSET + _BAND_HW
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
# CAMERA SMOOTHING ("bumping strength", F10 menu): a soft DAMP on the band-drag — the camera eases toward the
# band edge with an exponential time constant instead of sticking to it rigidly, so it trails the player and
# settles with a gentle bump. 0 == rigid (the pure band-drag). Level -> tau (s). The damping lag grows the
# deviation, so HIGH is sized to stay inside Y_V_PAD.
_Y_SMOOTH_TAU = (0.0, 0.05, 0.09, 0.14)         # Off / Low / Medium / High
Y_SMOOTH_LABELS = ("Off", "Low", "Medium", "High")
# Soft RE-CENTER time-constant (s): with the 1:1 velocity FOLLOW below, the dead-zone is no longer a hard hold
# (which let the player DRIFT across a static background during a slow platform ride — the judder) but a gentle
# pull that eases the player back toward the [_Y1,_Y2] band ONLY when they leave it, slowly enough never to
# judder. The "camera smoothing" dial makes it gentler still (max with the user tau).
_RECENTER_TAU = 0.35


def y_smooth_tau(level: int) -> float:
    """The band-drag damping time-constant (s) for a CAMERA-SMOOTHING menu level (clamped to range)."""
    return _Y_SMOOTH_TAU[min(max(int(level), 0), len(_Y_SMOOTH_TAU) - 1)]


def smooth_cam_y(sy: float, look: float, pwy: float, pvy: float, dt: float, cam_uninterp: float,
                 bottom_px: float, dos_follow: float | None = None, tau: float = 0.0,
                 v_pad: float = Y_V_PAD) -> tuple[float, float]:
    """One presentation-camera Y update (pure): the X band-drag, vertical, on the player's BODY CENTRE, +
    velocity look-ahead bias + optional smoothing damp. Returns ``(new_sy, new_look)``.

    ``sy``/``look`` = the presentation cam_y and the current look-ahead bias (state); ``pwy`` = the player's
    sub-tick interpolated world-y BASELINE (the record [0x4F1E]); ``pvy`` px/s (+ = falling); ``dt`` s since
    the last update; ``cam_uninterp`` = the CURRENT tick's shake-free DOS cam_y (``cam_y*16 + fine``, no
    [0x6BF8] jolt — the compositor slices the tile layer at ``cam_uninterp - present_cam_y``, so the
    extraction-coverage clamp is taken against it); ``bottom_px`` = the DOS bottom camera limit
    ([0x2CF5]-0xB tiles, px). ``tau`` = the CAMERA-SMOOTHING damping (s; 0 = rigid band-drag).

    ``dos_follow``: the FORCED-SCROLL escape — on a level whose camera header sets [0x8166]&4 the DOS camera
    auto-scrolls regardless of the player, and a band would fight it into the coverage clamp (stair-stepping);
    pass the interpolated shake-free DOS cam_y to follow it directly instead (smooth: it is interpolated).

    Rigid band target (identical shape to :func:`smooth_cam_x`): unchanged while the player's presented body
    sits inside the (bias-shifted) band, dragged by the exact overshoot outside — inherently smooth because
    the player position is sub-tick interpolated. GLIDE cap on seed/recovery; the DOS camera's own world
    bounds [0, bottom]; the coverage clamp is a pure safety (structural bound < v_pad-1)."""
    if dos_follow is not None:                         # forced auto-scroll level: follow the DOS cam directly
        sy, look = float(dos_follow), 0.0
        sy = min(max(sy, 0.0), max(0.0, bottom_px))
        lim = max(0.0, v_pad - 1.0)
        return min(max(sy, cam_uninterp - lim), cam_uninterp + lim), look
    # The level's airborne framing signal (DOS 5663 look-ahead), POSITION-gated like the original: the DOS only
    # LEADS (target row 3 falling / 8 rising) once the player REACHES the trigger row (dxs>=9 / <=2) — a small
    # jump that never leaves the dead-zone scrolls NOTHING (in a viewport-tall cave jumping never moves the DOS
    # camera at all). A pure VELOCITY bias instead engaged on any fall and dipped the view ~30px on every cave
    # jump. Gate the bias on the player's presented FEET passing the band EDGE (== the band-drag's own trigger),
    # so a jump WITHIN the band adds no look-ahead; only a real fall/rise past the band leads the view.
    feet = pwy - sy                                     # the player's presented feet screen-y
    if pvy > 0.0 and feet >= _Y2:                       # falling AND at/below the band bottom (DOS dxs>=9)
        want = min(pvy * LOOK_K, LOOK_DOWN)
    elif pvy < 0.0 and feet <= _Y1:                     # rising AND at/above the band top (DOS dxs<=2)
        want = max(pvy * LOOK_K, -LOOK_UP)
    else:
        want = 0.0
    db = want - look                                    # slew-limited engage/release (framerate-independent)
    lim = LOOK_RATE * max(0.0, dt)
    look += min(max(db, -lim), lim)
    # 1:1 VELOCITY FOLLOW (feed-forward): move the camera BY the player's own vertical displacement, so during
    # any sustained vertical motion — a platform ride, a fall — the player holds a FIXED screen row and the
    # BACKGROUND scrolls (coherent, smooth), instead of the player DRIFTING across a static background. That
    # slow isolated-sprite drift is the judder: the DOS camera's vertical DEAD-ZONE lets the player creep ~4px
    # /tick through 60px of screen before it scrolls (measured native_snap 005847: player screen-y 81->29 while
    # the DOS cam sat still) — a fast fall felt smooth only because it BLEW PAST the dead-zone and the DOS cam
    # tracked 1:1 (screen-y a constant 53). Feed-forward gives every sustained motion that same 1:1 lock.
    sy += pvy * dt
    # The band is now a SOFT RE-CENTER bound, not the tracker: only when the player's presented feet leave the
    # (bias-shifted) [_Y1,_Y2] dead-zone does the camera ease them back toward the edge — gently (_RECENTER_TAU,
    # or gentler under the smoothing dial) so re-centering itself never judders. Inside the zone the feed-forward
    # alone holds them steady, so a walk over gentle terrain or a platform ride shows NO drift.
    t = min(max(sy, pwy - _Y2 + look), pwy - _Y1 + look)
    t = min(max(t, 0.0), max(0.0, bottom_px))           # the DOS camera's own world bounds
    glide = (abs(pvy) + CATCHUP) * max(0.0, dt)         # glide cap: covers feed-forward + a CATCHUP-limited seed
    d = t - sy
    if dt > 0.0:                                        # gentle soft re-center (never instant -> no snap judder)
        d *= 1.0 - _exp(-dt / max(_RECENTER_TAU, tau))
    if d > glide:                                       # never exceed the glide cap (no snap on a big seed jump)
        d = glide
    elif d < -glide:
        d = -glide
    sy = min(max(sy + d, 0.0), max(0.0, bottom_px))
    lim = max(0.0, v_pad - 1.0)                         # extraction/re-admission coverage safety
    sy = min(max(sy, cam_uninterp - lim), cam_uninterp + lim)
    # INTEGER screen-offset LOCK — the reason X is pixel-perfect and Y was not. The compositor rounds the CAMERA
    # (bg_dy = round(cam - sy)) and each object's sub-tick INTERPOLATION (round(inv*wdy)) SEPARATELY; the two
    # roundings only CANCEL when the player's screen offset (pwy - sy) is an INTEGER — then every object that
    # moves with the player is pixel-static and the background still scrolls in smooth 1px steps (round(pwy)).
    # X gets this for free: its camera is player_x - X1 (X1 an integer band edge) and the horizontal camera is
    # tile-granular. Y's float feed-forward left the offset fractional, so the roundings beat by ±1px on EVERY
    # object (the platform shimmer), worse the faster the fractional part swept. Snapping the offset to an
    # integer removes the beat entirely. (sy stays smooth: it keeps pwy's fractional part, tracking it exactly.)
    return pwy - round(pwy - sy), look
