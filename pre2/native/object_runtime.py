"""The object-backed runtime — run gameplay on the offset-free object graph, not the byte image.

Mirrors :mod:`pre2.native.field_runtime` (the FieldBackend seam), one level further toward the north star:
gameplay ticks mutate real ``Player``/``Actor``/``Camera``/... dataclasses instead of named-but-still-byte-keyed
field slots. Two modes and one fold:

    to_object_store(state)  gameplay mode — mutations land on the object graph, off the image
    enter_image_mode(state) transition/level-load mode — those routines mix view + raw ``.data`` access (the
                            SAME reason FieldBackend needs this), so they must run wholly on the image: fold
                            the object graph in, then use a plain ByteBackend for the duration
    materialize(state)      fold the object graph's state into the image (before a render / digest / a
                            transition); a no-op when the state is already image-backed

**Why this is shipped and its bridge twin is now a shim** (2026-07-16, Stage 2.5 of
docs/pre2/offset_free_release_plan.md): this seam used to live only in ``pre2/bridge/object_runtime.py``, so
the product could not reach it (scripts/lint.py forbids ``pre2/native`` importing ``pre2.bridge``) and the
object graph could only ever run inside verification scripts — which is exactly why the shipped default stayed
the byte image however many modules were converted. With the construction moved to
:mod:`pre2.native.graph_layout`, the product can seed its own graph directly.

The object graph is the SOLE authority while gameplay runs here: un-converted callers reach state through
:class:`~pre2.native.object_state.ObjectGraphStore`'s ``rb``/``wb`` compat shim, which routes their offsets to
the same live objects — it is NOT a second copy synchronised against the image. (That distinction is the whole
lesson of the reverted transactional-Player experiment: two authorities reconciled by syncing always lets one
side silently clobber the other.) ``readonly_image=True`` enforces it — any un-routed mutable write raises.
"""
from __future__ import annotations

from pre2.native.graph_layout import DataclassBackend
from pre2.native.seams import RENDER_COUNTERS
from pre2.views.dgroup_view import ByteBackend

_DS_BASE = 0x1A0F << 4


def to_object_store(state, readonly_image: bool = True) -> None:
    """Put ``state`` on the object graph (seeded from the current image). Gameplay runs here."""
    state.backend = DataclassBackend(state, readonly_image=readonly_image)


def materialize(state) -> None:
    """Fold the object graph's GAMEPLAY state back into ``state.data`` so a renderer / digest / transition sees
    a whole, current DGROUP image. A no-op when the state is already image-backed.

    The render-owned per-frame counters (``RENDER_COUNTERS`` — the anim-remap throttle, the dither rotation, the
    ISR timer tick) are NOT gameplay state: the renderer steps them and they persist in the image across frames.
    A gameplay materialize must not reset them (doing so freezes the animated-tile remap / dither), so they are
    preserved across the fold. This is what lets the real render loop run every frame on the object store —
    proven byte-exact by scripts/verify_object_render.py."""
    mat = getattr(state.backend, "materialize", None)
    if mat is None:
        return
    base = _DS_BASE
    saved = [(o, state.data[base + o]) for o in RENDER_COUNTERS]
    mat(state.data)
    for o, v in saved:
        state.data[base + o] = v


def enter_image_mode(state) -> None:
    """Switch to running wholly on the byte image — for transitions / level-load, which mix named-object and
    raw ``.data`` access and so cannot straddle the object graph. Materialises first (no state is lost)."""
    materialize(state)
    state.backend = ByteBackend(state)


def is_object_backed(state) -> bool:
    """True when ``state`` is currently on the object graph (gameplay mode), not a plain byte image."""
    return isinstance(getattr(state, "backend", None), DataclassBackend)


class ObjectStore:
    """The gameplay-state-of-record controller the product's frame loop drives
    (``native_frame_step_tagged``'s default). ``seed`` puts the tick on the object graph — fresh from the
    current image each frame, so it picks up whatever the prior frame's render/transition left; ``fold`` folds
    the ticked objects back into the image (preserving the render-owned counters) and returns the state to the
    byte image for the render + any transition.

    Shipped counterpart of ``pre2/bridge/object_runtime.ObjectStore``, which predates this module and exists
    because the product could not import the bridge; the loop takes it by dependency injection either way."""

    __slots__ = ("readonly_image",)

    def __init__(self, readonly_image: bool = True) -> None:
        self.readonly_image = readonly_image

    def seed(self, state) -> None:
        to_object_store(state, readonly_image=self.readonly_image)

    def fold(self, state) -> None:
        enter_image_mode(state)
