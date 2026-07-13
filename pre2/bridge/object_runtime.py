"""The object-backed runtime — run gameplay on the offset-free object graph, not the byte image.

Mirrors ``pre2.native.field_runtime`` (the FieldBackend seam), one level further toward the north star: gameplay
ticks mutate real ``Player``/``Actor``/``Camera``/... dataclasses (:class:`~pre2.bridge.game_layout.DataclassBackend`)
instead of named-but-still-byte-keyed field slots. Two modes and one fold:

    to_object_store(state)  gameplay mode — mutations land on the object graph, off the image
    enter_image_mode(state) transition/level-load mode — those routines mix view + raw ``.data`` access (the
                            SAME reason FieldBackend needs this), so they must run wholly on the image: fold
                            the object graph in, then use a plain ByteBackend for the duration
    materialize(state)      fold the object graph's state into the image (before a render / digest / a
                            transition); a no-op when the state is already image-backed

The original DOS memory FORMAT lives entirely on the other side of this seam (the bridge layout tables). Attach
it and the object-backed game is byte-exact verifiable against the original across a whole playthrough,
transitions included (``scripts/verify_object_finish.py``). Detach it and the game runs on objects that need
none of it.
"""
from __future__ import annotations

from pre2.bridge.game_layout import DataclassBackend
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
