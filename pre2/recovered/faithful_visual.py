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

__all__ = ["SceneKind", "render_visual"]


class SceneKind(IntEnum):
    GAMEPLAY = 0   # normal scrolling gameplay frame -> render_frame
    IRIS = 1       # end-level circular iris over the gameplay frame -> render_frame + compose_iris
    IMAGE = 2      # intro / title (mode 13h linear) -> leaf NOT recovered yet
    SCENE = 3      # menu / map / loading / tally / game-over (mode 0Dh planar) -> leaf NOT recovered yet


def render_visual(scene_kind: SceneKind, rs, planes, *, iris=None, dac=None) -> bool:
    """Compose one faithful frame for ``scene_kind`` into ``planes`` (clean framebuffer).

    Returns ``True`` if rendered faithfully, ``False`` if the scene's leaf is not recovered yet (the
    caller should fall back to the VM's own frame). ``rs`` is the gameplay ``RendererState``; ``iris``
    (for ``IRIS``) is a duck-typed object carrying the iris compose inputs (``src_x``/``src_y``/
    ``scale``/``x_off``/``y_off``/``x_clamp``/``tbl_x``/``tbl_y``/``page``)."""
    if scene_kind == SceneKind.GAMEPLAY:
        render_frame(rs, planes, dac, rebuild=True)
        return True
    if scene_kind == SceneKind.IRIS:
        render_frame(rs, planes, dac, rebuild=True)            # the base gameplay frame
        if iris is not None:                                  # clear everything outside the iris circle
            compose_iris(planes, iris.src_x, iris.src_y, iris.scale, iris.x_off, iris.y_off,
                         iris.x_clamp, iris.tbl_x, iris.tbl_y, iris.page)
        return True
    return False    # IMAGE / SCENE leaves not recovered yet -> caller falls back to the VM frame
