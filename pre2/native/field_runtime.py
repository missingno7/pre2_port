"""The field-backed runtime — run the game on the named FieldBackend store, not the byte image.

Gameplay ticks mutate named state in a :class:`~pre2.views.dgroup_view.FieldBackend`; the byte image is only
ever a load / render / serialise buffer. Two modes and one fold:

    to_field_store(state)   gameplay mode — named writes land in the field store, off the image
    enter_image_mode(state) transition/level-load mode — those routines mix view + raw ``.data`` access, so
                            they must run wholly on the image: fold the field store in, then use a plain
                            ByteBackend for the duration
    materialize(state)      fold the field store's named bytes into the image (before a render / digest /
                            snapshot read); a no-op when the state is already image-backed

THE UMBILICAL CORD. The original DOS memory FORMAT lives entirely on the other side of this seam. Attach the
VM oracle + the serialiser (``pre2/bridge/state_fields.py``) and the field-backed game is byte-exact
verifiable against the original — proven across a whole playthrough by ``scripts/verify_hybrid_finish.py``.
Detach them and the game runs on named state that needs none of it. The image is the plug; the names are the
game.
"""
from __future__ import annotations

from pre2.views.dgroup_view import ByteBackend, FieldBackend, HybridBackend


def to_field_store(state) -> None:
    """Put ``state`` on the named field store (seeded from the current image). Gameplay runs here."""
    state.backend = HybridBackend(FieldBackend(state), state.data)


def materialize(state) -> None:
    """Fold the field store's named bytes back into ``state.data`` so a renderer / digest / snapshot sees a
    whole, current DGROUP image. A no-op when the state is already image-backed."""
    mat = getattr(state.backend, "materialize", None)
    if mat is not None:
        mat(state.data)


def enter_image_mode(state) -> None:
    """Switch to running wholly on the byte image — for transitions / level-load, which mix named-view and
    raw ``.data`` access and so cannot straddle the field store. Materialises first (no state is lost)."""
    materialize(state)
    state.backend = ByteBackend(state)


def is_field_backed(state) -> bool:
    """True when ``state`` is currently on the field store (gameplay mode), not a plain byte image."""
    return getattr(state.backend, "materialize", None) is not None
