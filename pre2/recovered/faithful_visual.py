"""Faithful visual dispatcher — route a frame to the right recovered visual leaf by scene kind.

This is the single composition entry ABOVE ``render_frame``. It does NOT replace ``render_frame``
(the gameplay-frame composer); it ORCHESTRATES the recovered leaves so the WHOLE visual flow —
gameplay + transitions + (later) scenes — is one coherent faithful layer instead of a gameplay-only
composer. It reuses the SAME recovered functions the checkpoints/probes verify (no second copy):
``render_frame`` (gameplay) and ``compose_iris`` (the end-level iris over the gameplay frame).

Pure: no ``cpu``/``mem`` imports. The bridge derives the :class:`SceneKind` + the per-scene inputs
(``pre2.bridge.scene_state``) and feeds them here. ``render_visual`` itself composes ``GAMEPLAY`` and
``IRIS`` from recovered leaves; for ``IMAGE``/``SCENE`` it raises :class:`FaithfulVisualGap`. The viewer's
faithful path (``scripts/play.py``) catches that and composes the RECOVERED non-gameplay scenes from their
own recovered leaves — 13h images (``bridge.image_scene.render_image_scene``), game-over / tally / OLDIES
(``scene_capture`` from ``build_*_scene``) — and surfaces the gap LOUDLY only for the still-unrecovered
0Dh scrolling compositions (mode-select menu, map/carte; blocked on a history-dependent buffer). There is
**no VM-framebuffer fallback** anywhere on the faithful path. (Folding the recovered scene paths up into
``render_visual`` itself is a convergence TODO.)
"""
from __future__ import annotations

from enum import IntEnum

from pre2.recovered.render_frame import render_frame
from pre2.recovered.transition import compose_iris

__all__ = ["SceneKind", "FaithfulVisualGap", "render_visual"]


class SceneKind(IntEnum):
    GAMEPLAY = 0   # normal scrolling gameplay frame -> render_frame
    IRIS = 1       # end-level circular iris over the gameplay frame -> render_frame + compose_iris
    IMAGE = 2      # intro / title (mode 13h linear) -> composed by the viewer's render_image_scene path
    SCENE = 3      # 0Dh planar: game-over/tally/OLDIES composed via scene_capture; menu/map still blocked


# render_visual handles GAMEPLAY/IRIS; for IMAGE/SCENE the viewer's faithful path composes the recovered
# scenes and only the still-unrecovered 0Dh compositions reach this gap. The hint names what remains.
_GAP_HINT = {
    SceneKind.IMAGE: "13h IMAGE — composed by the viewer's faithful 13h path (bridge.image_scene."
                     "render_image_scene); render_visual itself only does GAMEPLAY/IRIS (fold-in is a TODO)",
    SceneKind.SCENE: "0Dh SCENE — game-over/tally/OLDIES are composed via scene_capture; the mode-select "
                     "menu + map/carte COMPOSITIONS are BLOCKED on a history-dependent buffer (recover the "
                     "initial full-page-fill producer + a persistent-page model; do NOT rebuild from scratch)",
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

    Composes ``GAMEPLAY`` and ``IRIS`` here; raises :class:`FaithfulVisualGap` for ``IMAGE``/``SCENE``
    (the viewer's faithful path composes the recovered ones — 13h images, game-over, tally, OLDIES — and
    only the unrecovered 0Dh compositions surface the gap). NO silent VM-framebuffer fallback, ever. ``rs``
    is the gameplay ``RendererState``; ``iris``
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
    raise FaithfulVisualGap(scene_kind)    # IMAGE/SCENE: handled by the viewer's scene paths, else loud gap
