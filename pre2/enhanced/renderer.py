"""EnhancedRenderer — the modern presentation backend (``--video enhanced``).

It consumes the FAITHFUL output (the recovered :class:`~pre2.bridge.faithful_session.FaithfulSession`) and
projects it through a modern RGB/RGBA pipeline. It is a *projection of recovered state*, never a second VM
renderer:

  * It is handed only the already-composed faithful frame + the session's grounded source snapshots
    (``enh_prev``/``enh_cur`` EnhancedFrameState). It NEVER reads the VM framebuffer / ``mem`` / ``dos`` /
    CPU — the forbidden "read A000 on miss" fallback is structurally impossible.
  * It NEVER advances game state, writes VM memory, or changes gameplay/timing — output only.
  * When the grounded source state is missing / stale / non-gameplay, or interpolation is disabled, it
    returns the **faithful frame unchanged** (the only allowed fallback) and reports why.

This milestone: gameplay-only object-aware interpolation via :func:`pre2.enhanced.compositor.compose` —
background + per-sprite RGBA blits at ``base_id``-matched interpolated positions, at the display refresh,
between ~25 fps source frames. Everything else (HUD strip, menu, CARTE, scenes, transitions) is faithful
passthrough. No truecolor fade / iris / curtain / smooth-camera yet.
"""
from __future__ import annotations

from pre2.enhanced.compositor import compose

# Gameplay source frames commit ~25 fps (~40 ms). If the latest source snapshot is older than this, or the
# prev->cur interval is this large, we are not in steady gameplay (a scene, a load, a pause) -> passthrough
# rather than interpolate across a gap.
_MAX_SOURCE_GAP = 0.12   # seconds


class EnhancedRenderer:
    def __init__(self, faithful_source, *, interpolate=True, options=None):
        self.src = faithful_source          # FaithfulSession — grounded snapshots only; NEVER mem/dos
        self.interpolate = interpolate
        self.options = dict(options or {})
        self._diag = {"interpolated_sprites": 0, "passthrough": True,
                      "alpha": 0.0, "reason": "init"}

    def present(self, now, faithful_frame):
        """Return the display frame at wall time ``now``: an interpolated gameplay composite when the
        grounded source snapshots support it, else the faithful frame unchanged."""
        s = self.src
        cur, prev = s.enh_cur, s.enh_prev
        reason = None
        if not self.interpolate:
            reason = "interpolation disabled"
        elif cur is None:
            reason = "no source snapshot (non-gameplay / not yet captured)"
        elif (now - s.enh_cur_time) > _MAX_SOURCE_GAP:
            reason = "source snapshot stale (non-gameplay / paused)"
        if reason is not None:
            self._diag = {"interpolated_sprites": 0, "passthrough": True, "alpha": 1.0, "reason": reason}
            return faithful_frame
        period = s.enh_cur_time - s.enh_prev_time
        if prev is None or period <= 0.0 or period > _MAX_SOURCE_GAP:
            # first gameplay frame, or a large gap (scene->gameplay resume): show current, don't interpolate
            self._diag = {"interpolated_sprites": 0, "passthrough": False, "alpha": 1.0,
                          "reason": "no prior source frame to interpolate from", "unsupported": len(cur.unsupported)}
            return compose(cur, None, 1.0)
        alpha = (now - s.enh_cur_time) / period
        alpha = 0.0 if alpha < 0.0 else 1.0 if alpha > 1.0 else alpha
        frame = compose(cur, prev, alpha)
        self._diag = {"interpolated_sprites": sum(1 for sp in cur.sprites if sp.interpolate),
                      "passthrough": False, "alpha": alpha, "reason": "object-interpolated",
                      "unsupported": len(cur.unsupported)}
        return frame

    def active_enhancements(self) -> dict:
        """Diagnostic flags + the last :meth:`present` decision."""
        d = dict(self._diag)
        d.update(native_refresh_output=not d["passthrough"], frame_interpolation=not d["passthrough"],
                 object_interpolation=not d["passthrough"], truecolor_palette_fade=False,
                 iris_projection=False, curtain_projection=False,
                 faithful_passthrough_reason=(d["reason"] if d["passthrough"] else None))
        return d

    def status(self) -> str:
        """Short title-bar tag of what's active."""
        d = self._diag
        if d["passthrough"]:
            return f"enh:passthrough ({d['reason']})"
        u = d.get("unsupported", 0)
        return (f"enh:interp a={d['alpha']:.2f} sprites={d['interpolated_sprites']}"
                + (f" unsupported={u}" if u else ""))
