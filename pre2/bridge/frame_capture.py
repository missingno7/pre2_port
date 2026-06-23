"""The per-tick capture seam: a rolling window of the last two render snapshots.

Driven once per game frame (by the play loop or a probe — no VM hook needed): each tick reads
the renderer's state from memory and assembles a :class:`~pre2.recovered.render_model.
GameFrameSnapshot`, keeping the last two. ``interpolated(t)`` then produces an intermediate
frame — the bridge a future enhanced renderer consumes to draw on its own (higher) clock.

This is layout/glue only; the snapshot assembly lives in ``pre2.recovered.render_snapshot`` and
the interpolation in ``pre2.recovered.render_interp``.
"""
from __future__ import annotations

from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_interp import interpolate_frame
from pre2.recovered.render_snapshot import build_frame_snapshot


class FrameCapture:
    """Rolling last-two GameFrameSnapshots for inter-frame interpolation."""

    def __init__(self) -> None:
        self._frames: list = []

    def tick(self, mem, dos=None):
        """Capture this frame's snapshot (call once per game frame). Pass ``dos`` to include the
        displayed palette colours. Returns the new snapshot."""
        snap = build_frame_snapshot(read_renderer_state(mem, dos))
        self._frames.append(snap)
        if len(self._frames) > 2:
            self._frames.pop(0)
        return snap

    @property
    def cur(self):
        """The most recent captured snapshot (or None)."""
        return self._frames[-1] if self._frames else None

    @property
    def prev(self):
        """The snapshot before ``cur`` (or None until two have been captured)."""
        return self._frames[-2] if len(self._frames) >= 2 else None

    def interpolated(self, t: float):
        """An intermediate frame at ``t`` in [0,1] between ``prev`` and ``cur`` (``cur`` if only
        one frame has been captured)."""
        return interpolate_frame(self.prev, self.cur, t)
