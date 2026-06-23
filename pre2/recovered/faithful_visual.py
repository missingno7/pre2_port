"""Faithful visual dispatcher — route a frame to the right recovered visual leaf by scene kind.

This is the single composition entry ABOVE ``render_frame``. It does NOT replace ``render_frame``
(the gameplay-frame composer); it ORCHESTRATES the recovered leaves so the WHOLE visual flow —
gameplay + transitions + (later) scenes — is one coherent faithful layer instead of a gameplay-only
composer. It reuses the SAME recovered functions the checkpoints/probes verify (no second copy):
``render_frame`` (gameplay) and ``compose_iris`` (the end-level iris over the gameplay frame).

Pure: no ``cpu``/``mem`` imports. The bridge derives the :class:`SceneKind` + the per-scene inputs
(``pre2.bridge.scene_state``) and feeds them here. ``IMAGE``/``SCENE`` leaves are not recovered yet,
so ``render_visual`` returns ``False`` for them and the caller falls back to the VM frame.
"""
from __future__ import annotations

from enum import IntEnum

from pre2.recovered.render_frame import render_frame
from pre2.recovered.transition import compose_iris

__all__ = ["SceneKind", "FaithfulVisualGap", "render_visual"]


class SceneKind(IntEnum):
    GAMEPLAY = 0   # normal scrolling gameplay frame -> render_frame
    IRIS = 1       # end-level circular iris over the gameplay frame -> render_frame + compose_iris
    IMAGE = 2      # intro / title (mode 13h linear) -> leaf NOT recovered yet
    SCENE = 3      # menu / map / loading / tally / game-over (mode 0Dh planar) -> leaf NOT recovered yet


# What each not-yet-recovered scene needs, so the gap names exactly what is missing.
_GAP_HINT = {
    SceneKind.IMAGE: "intro/title IMAGE scene (mode 13h linear) — recover the linear-image scene leaf "
                     "(bridge/scene_state image inputs + a render_image leaf) and wire it into render_visual",
    SceneKind.SCENE: "menu/map/loading/tally/game-over SCENE (mode 0Dh planar) — wire the recovered "
                     "render_scene + draw_string leaves (+ a bridge SceneState reader) into render_visual",
}


class FaithfulVisualGap(RuntimeError):
    """The faithful visual layer reached a scene whose recovered leaf does not exist yet.

    Raised LOUD instead of silently falling back to the ASM-populated VRAM — a silent fallback would
    HIDE exactly the missing visual work we must complete (the "no silent fallback" rule). Carries the
    :class:`SceneKind` and a precise hint of what to recover."""

    def __init__(self, scene_kind: SceneKind):
        self.scene_kind = scene_kind
        super().__init__(f"faithful visual gap: {scene_kind.name} not recovered — "
                         f"{_GAP_HINT.get(scene_kind, 'recover its leaf and wire it into render_visual')}")


def render_visual(scene_kind: SceneKind, rs, planes, *, iris=None, dac=None) -> None:
    """Compose one faithful frame for ``scene_kind`` into ``planes`` (clean framebuffer).

    Raises :class:`FaithfulVisualGap` if the scene's leaf is not recovered yet — NO silent fallback,
    so the missing visual work is named exactly. ``rs`` is the gameplay ``RendererState``; ``iris``
    (for ``IRIS``) is a duck-typed object carrying the iris compose inputs (``src_x``/``src_y``/
    ``scale``/``x_off``/``y_off``/``x_clamp``/``tbl_x``/``tbl_y``/``page``)."""
    if scene_kind == SceneKind.GAMEPLAY:
        render_frame(rs, planes, dac, rebuild=True)
        return
    if scene_kind == SceneKind.IRIS:
        render_frame(rs, planes, dac, rebuild=True)            # the base gameplay frame
        if iris is not None:                                  # clear everything outside the iris circle
            compose_iris(planes, iris.src_x, iris.src_y, iris.scale, iris.x_off, iris.y_off,
                         iris.x_clamp, iris.tbl_x, iris.tbl_y, iris.page)
        return
    raise FaithfulVisualGap(scene_kind)    # IMAGE / SCENE leaves not recovered yet — fail loud
