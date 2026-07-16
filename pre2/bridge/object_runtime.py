"""The object-backed runtime seam — re-export shim + the bridge's injection controller.

**The seam itself moved to :mod:`pre2.native.object_runtime` on 2026-07-16** (Stage 2.5 of
docs/pre2/offset_free_release_plan.md), together with the layout it needs
(:mod:`pre2.native.graph_layout`). While it lived here the product could not reach it — scripts/lint.py forbids
``pre2/native`` importing ``pre2.bridge`` — so the object graph only ever ran inside verification scripts and
the shipped default stayed the byte image. ``to_object_store``/``materialize``/``enter_image_mode``/
``is_object_backed`` are re-exported below so existing bridge-side and script importers keep working unchanged.

What stays here is :class:`ObjectStore`, the dependency-injection controller
``native_frame_step_tagged(..., store=...)`` takes. That indirection exists because the product's frame loop
must not depend on the object model; it remains valid, and is now simply a thin wrapper over the shipped seam.
"""
from __future__ import annotations

from pre2.native.object_runtime import (  # noqa: F401
    enter_image_mode, is_object_backed, materialize, to_object_store,
)


class ObjectStore:
    """The gameplay-state-of-record controller the product's frame loop calls via dependency injection
    (``native_frame_step_tagged(..., store=ObjectStore())``). The product never imports this — the bridge is
    detachable and hands it in — so the shipped loop carries no dependency on the object model.

    ``seed`` puts the tick on the object graph (fresh from the current image each frame, so it picks up whatever
    the prior frame's render/transition left); ``fold`` folds the ticked objects back into the image (preserving
    the render-owned counters) and returns the state to the byte image for the render + any transition."""

    __slots__ = ("readonly_image",)

    def __init__(self, readonly_image: bool = True) -> None:
        self.readonly_image = readonly_image

    def seed(self, state) -> None:
        to_object_store(state, readonly_image=self.readonly_image)

    def fold(self, state) -> None:
        enter_image_mode(state)
