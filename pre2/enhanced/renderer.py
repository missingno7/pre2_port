"""EnhancedRenderer — the modern presentation backend (``--video enhanced``).

It consumes the FAITHFUL output (the recovered :class:`~pre2.bridge.faithful_session.FaithfulSession`) and
projects it through a modern pipeline. It is a *projection of recovered state*, never a second VM renderer:

  * It NEVER receives or reads the VM framebuffer / ``mem`` / ``dos`` — it is handed only the already-composed
    faithful frame (and, later, a grounded state snapshot pulled from the faithful source). The forbidden
    "read A000 on miss" fallback is structurally impossible: there is no VM handle here.
  * It NEVER advances game state, writes VM memory, or changes gameplay/timing — output only.
  * When an enhancement's grounded source state is unavailable it returns the **faithful frame unchanged**
    (the only allowed fallback), and reports why via :meth:`active_enhancements`.

Milestone 2 (this file) is pure **passthrough**: ``render`` returns the faithful frame unchanged, proving the
backend boundary. Later milestones add, one at a time and each grounded:
  3. truecolor palette-fade projection · 4. iris/curtain transition projection · 5. native-refresh + frame/
  object interpolation. Smooth camera is deferred (it needs world/camera semantics, not a display filter).
"""
from __future__ import annotations

# The enhancement layers this renderer can report as active (diagnostics the user asked for).
_FLAGS = ("native_refresh_output", "frame_interpolation", "object_interpolation",
          "truecolor_palette_fade", "iris_projection", "curtain_projection")


class EnhancedRenderer:
    def __init__(self, faithful_source, options=None):
        self.src = faithful_source          # FaithfulSession — for grounded state queries; NEVER mem/dos
        self.options = dict(options or {})
        self._active = {f: False for f in _FLAGS}
        self._active["faithful_passthrough_reason"] = "no enhancements enabled"

    def render(self, faithful_frame, *, now=None):
        """Project one faithful frame through the enhancement pipeline and return the presentation frame.

        Milestone 2: **passthrough** — return ``faithful_frame`` unchanged (present sentinels included, so the
        caller's blank/unknown handling is identical to ``--video faithful``). Future milestones insert the
        grounded projections here (each falling back to ``faithful_frame`` when its source state is missing).
        ``faithful_frame`` is the composed faithful RGB or a present sentinel — never the VM framebuffer."""
        for f in _FLAGS:
            self._active[f] = False
        self._active["faithful_passthrough_reason"] = "passthrough (no enhancements enabled yet)"
        return faithful_frame

    def active_enhancements(self) -> dict:
        """The diagnostic flags + ``faithful_passthrough_reason`` for the last :meth:`render`."""
        return dict(self._active)

    def status(self) -> str:
        """Short title-bar tag: the active enhancement layers, or ``passthrough``."""
        on = [f for f in _FLAGS if self._active.get(f)]
        return "enh:" + ("+".join(on) if on else "passthrough")
