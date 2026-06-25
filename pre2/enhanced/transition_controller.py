"""Native enhanced transition controller — present-time state machines for scene transitions.

The model (per the enhanced-renderer design): the recovered runtime state only TRIGGERS a transition and
supplies its semantic parameters (kind, centre, direction, old/new scene). The enhanced controller then owns
the presentation-time state and renders the effect EVERY display frame, computing progress from the wall
clock -- smooth by construction, not a faithful phase sampled at source FPS and interpolated afterwards.

Phases (the controller decides what is visible in each, so the new scene is never shown early -- no blink):

    CLOSE    : the old scene with the effect closing      (progress 0->1 over the close duration)
    COVERED  : fully closed (black, + the iris's kept sprite) while the game loads the new scene behind it
    OPEN     : the new scene with the effect opening       (progress 0->1 over the open duration)

Grounding: same trigger, same scene meaning, same old/new relationship, same general timing/dramaturgy; no
invented gameplay state, no VM framebuffer. The visuals may be smoother/cleaner than the EGA original.
"""
from __future__ import annotations

import numpy as np

from pre2.enhanced.compositor import _blit
from pre2.enhanced.transitions import apply_curtain, apply_iris, apply_vfade

# Close durations in PRESENT seconds, grounded by the recovered effect's natural length (the iris closes
# 0xE6->0 over ~48 source frames). The enhanced renders at the display rate from present-time progress, but is
# ANCHORED to the recovered radius as a floor (never LESS closed than the game actually is) so it always
# reaches black by the time the game's iris ends -- it can't be cut off by the tally, regardless of the exact
# live source cadence. Kept a touch faster than the measured pace so the present-time curve is the smooth
# driver and the anchor only catches the tail.
_IRIS_CLOSE_S = 1.2
_IRIS_R0 = 0xE6            # iris start radius (the recovered [0x2DD0] seed)
_COVERED_RELEASE_S = 0.6   # max covered-black hold after the recovered effect ends, before releasing the scene
# Room/cave transition (close = vertical fade-out, then a center-out curtain reveal of the new room). Present
# durations are the smooth driver; both phases are ANCHORED to the recovered progress (never less closed /
# less revealed than the game) so they always finish in step with the game, never cut off or lagging.
_VFADE_CLOSE_S = 0.5
_CURTAIN_OPEN_S = 0.6
_VFADE_MID = 88           # the two fade bands meet here (fully closed)
_CURTAIN_FULL = 10        # completed_pairs at a full reveal


class EnhancedTransition:
    """One active presentation-time transition. Created on trigger; rendered at present_hz until released."""

    def __init__(self, kind, start_time, *, old_frame=None, center=None, sprites=()):
        self.kind = kind                 # 'iris' (vfade / curtain to follow)
        self.start_time = start_time
        self.old_frame = old_frame       # frozen RGB of the scene being closed
        self.center = center             # (col, row) for the iris
        self.sprites = list(sprites)     # world sprites kept visible through the effect (the player)
        self.phase = "close"             # close -> covered -> open -> (release)
        self._covered_at = None          # wall time the effect finished closing / the recovered state ended
        self._anchor_radius = _IRIS_R0   # the game's CURRENT recovered radius (a floor on how closed we are)
        # room transition (close = vfade, open = curtain reveal of the new room)
        self.new_frame = None            # the new room, captured when the curtain (open) starts
        self._vf_anchor = (0, _VFADE_MID * 2)   # recovered vfade (top, bot) -- a floor on how closed we are
        self._curtain_anchor = 0.0       # recovered curtain completed_pairs -- a floor on how revealed we are
        self._open_start = None          # wall time the curtain (open) phase began

    # -- the recovered state drives only trigger/parameters/end, never the per-frame progress --
    def note_active(self, now, cur):
        """Called while the recovered effect is still active (keeps sprites/centre + the radius anchor fresh)."""
        if self.kind == "iris" and cur is not None and cur.iris is not None:
            self.center = (cur.iris.center_y, cur.iris.center_x)
            self._anchor_radius = cur.iris.radius
            if cur.sprites:
                self.sprites = [s for s in cur.sprites if s.interpolate]

    def note_vfade(self, top, bot):
        """Room CLOSE phase: the recovered vertical-fade bands (a floor on how closed we must be)."""
        self._vf_anchor = (top, bot)

    def note_curtain(self, now, completed, new_frame):
        """Room OPEN phase: the recovered curtain reveal started -> capture the new room + progress anchor."""
        if self.phase in ("close", "covered"):
            self.phase = "open"
            self._open_start = now
            if self.new_frame is None and new_frame is not None:
                self.new_frame = new_frame
        self._curtain_anchor = max(self._curtain_anchor, float(completed))

    def note_ended(self, now):
        """The recovered effect ended (scene is changing) -> enter the covered-black hold if not already.
        For a room transition mid-CLOSE this is the covered/black gap before the curtain opens."""
        if self._covered_at is None:
            self._covered_at = now
        if self.phase == "close":
            self.phase = "covered"

    def released(self, now, scene_ready, gameplay_fresh=False):
        """True when the controller should hand off (release the transition)."""
        if self.kind == "room":
            if self.phase == "open":
                # the new room is revealed -> hand back to the live game when it resumes, or the reveal is done
                return gameplay_fresh or ((now - self._open_start) >= _CURTAIN_OPEN_S
                                          and self._curtain_anchor >= _CURTAIN_FULL)
            if self.phase == "covered":
                # waiting for the curtain (cave) -- do NOT release on fresh gameplay (that is the blink); release
                # only if a real scene arrives (death -> game-over) or the covered-black hold expires.
                return scene_ready or (now - self._covered_at) >= _COVERED_RELEASE_S
            return False
        # iris
        if self.phase != "covered":
            return False
        return scene_ready or (now - self._covered_at) >= _COVERED_RELEASE_S

    def render(self, now):
        """Render this transition's frame at the wall time ``now`` (present_hz)."""
        if self.kind == "iris":
            return self._render_iris(now)
        if self.kind == "room":
            return self._render_room(now)
        return None

    def _render_room(self, now):
        if self.phase == "close":          # vertical fade-out closing the old room (present-time + anchor)
            p = min(1.0, max(0.0, (now - self.start_time) / _VFADE_CLOSE_S))
            top = max(int(_VFADE_MID * p), int(self._vf_anchor[0]))
            bot = min(int(2 * _VFADE_MID - _VFADE_MID * p), int(self._vf_anchor[1]))
            frame = self.old_frame.copy()
            apply_vfade(frame, top, bot)
            return frame
        if self.phase == "covered":        # black while the new room loads (never show it early -> no blink)
            return np.zeros_like(self.old_frame)
        # open: center-out curtain reveal of the new room (present-time + anchor to the recovered progress)
        p = min(1.0, max(0.0, (now - self._open_start) / _CURTAIN_OPEN_S))
        pairs = max(_CURTAIN_FULL * p, self._curtain_anchor)
        base = self.new_frame if self.new_frame is not None else self.old_frame
        return apply_curtain(np.zeros_like(base), base, pairs)

    def _render_iris(self, now):
        if self.phase == "close":
            p = min(1.0, max(0.0, (now - self.start_time) / _IRIS_CLOSE_S))
            # present-time progress, ANCHORED to the recovered radius so we are never less closed than the game
            # (so the tally can't cut us off mid-close); the present-time curve is the smooth driver otherwise.
            radius = min(_IRIS_R0 * (1.0 - p), float(self._anchor_radius))
            frame = self.old_frame.copy()
            apply_iris(frame, radius, self.center[0], self.center[1])
        else:  # covered: fully closed -> black, the player still visible (matches the VM)
            frame = np.zeros_like(self.old_frame)
        for inst in self.sprites:                 # the player stays visible throughout the iris + covered hold
            _blit(frame, inst.rgba, inst.screen_x + inst.tex_off_x, inst.screen_y + inst.tex_off_y)
        return frame
