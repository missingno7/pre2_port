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
from pre2.enhanced.transitions import apply_iris

# Close durations in PRESENT seconds, grounded by the recovered effect's natural length (e.g. the iris closes
# 0xE6->0 over ~48 source frames -> ~1.6s at the live source cadence). The enhanced effect tracks this pace
# but renders at the display rate; it then HOLDS covered-black until the recovered scene actually changes.
_IRIS_CLOSE_S = 1.6
_COVERED_RELEASE_S = 0.6   # max covered-black hold after the recovered effect ends, before releasing the scene


class EnhancedTransition:
    """One active presentation-time transition. Created on trigger; rendered at present_hz until released."""

    def __init__(self, kind, start_time, *, old_frame=None, center=None, sprites=()):
        self.kind = kind                 # 'iris' (vfade / curtain to follow)
        self.start_time = start_time
        self.old_frame = old_frame       # frozen RGB of the scene being closed
        self.center = center             # (col, row) for the iris
        self.sprites = list(sprites)     # world sprites kept visible through the effect (the player)
        self.phase = "close"             # close -> covered -> (release)
        self._covered_at = None          # wall time the effect finished closing / the recovered state ended

    # -- the recovered state drives only trigger/parameters/end, never the per-frame progress --
    def note_active(self, now, cur):
        """Called while the recovered effect is still active (keeps sprites/centre fresh)."""
        if self.kind == "iris" and cur is not None and cur.iris is not None:
            self.center = (cur.iris.center_y, cur.iris.center_x)
            if cur.sprites:
                self.sprites = [s for s in cur.sprites if s.interpolate]

    def note_ended(self, now):
        """The recovered effect ended (scene is changing) -> enter the covered-black hold if not already."""
        if self._covered_at is None:
            self._covered_at = now
        self.phase = "covered"

    def released(self, now, scene_ready):
        """True when the controller should hand off to the new scene (release the transition)."""
        if self.phase != "covered":
            return False
        # release as soon as the new scene is actually ready, else after a bounded covered-black hold
        return scene_ready or (now - self._covered_at) >= _COVERED_RELEASE_S

    def render(self, now):
        """Render this transition's frame at the wall time ``now`` (present_hz)."""
        if self.kind == "iris":
            return self._render_iris(now)
        return None

    def _render_iris(self, now):
        if self.phase == "close":
            p = min(1.0, max(0.0, (now - self.start_time) / _IRIS_CLOSE_S))
            radius = 0xE6 * (1.0 - p)
            frame = self.old_frame.copy()
            apply_iris(frame, radius, self.center[0], self.center[1])
        else:  # covered: fully closed -> black, the player still visible (matches the VM)
            frame = np.zeros_like(self.old_frame)
        for inst in self.sprites:                 # the player stays visible throughout the iris + covered hold
            _blit(frame, inst.rgba, inst.screen_x + inst.tex_off_x, inst.screen_y + inst.tex_off_y)
        return frame
