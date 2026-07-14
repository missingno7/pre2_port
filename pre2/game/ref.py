"""Offset-free references — the shipped object model's replacement for stored DGROUP pointers.

A DOS pointer field holds a 16-bit DGROUP offset the game dereferences. The object model instead holds one of
these typed references, which carry NO offset (an ``ObjectRef`` names a pool + index — ``actors[3]`` — not
``0x4FD0 + 3*0x12``). The detachable bridge (``pre2/bridge/pointer_layout``) owns the only offset<->ref map; it
swizzles a ref back to the exact offset when serialising to a byte image for verification, and offset->ref when
parsing. See docs/pre2/pointer_swizzle_design.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectRef:
    """A reference to one record of an object POOL, by pool name + index — ``actors[3]``, not an offset."""

    pool: str
    index: int


@dataclass(frozen=True)
class RawRef:
    """The OPAQUE fallback: a stored pointer we have not reverse-engineered yet, or a stale (freed-slot) pointer
    that is serialised but never dereferenced. Holds the raw 16-bit value verbatim with NO semantic claim, so it
    round-trips byte-exactly. The pointer analog of a placeholder field name — it makes incremental swizzling
    safe: whatever is still a ``RawRef`` at the end is the honest, enumerated residue."""

    value: int


#: the sentinel a pool pointer uses for "no object" (the dead-slot marker); deref treats it (and 0) as None.
NULL_SENTINEL = 0xFFFF


def is_null(ref) -> bool:
    """True when ``ref`` denotes 'no object' — a RawRef holding the 0xFFFF/0 sentinel."""
    return isinstance(ref, RawRef) and ref.value in (0x0000, NULL_SENTINEL)


def deref(ref, pools):
    """Resolve an ``ObjectRef`` to its live record — ``pools[ref.pool][ref.index]``. Offset-free: it just indexes
    the object lists. ``None`` for a null ref; a ``RawRef`` cannot be resolved without the bridge (it is opaque),
    so it raises — that is the loud signal that a pointer still needs swizzling."""
    if is_null(ref):
        return None
    if isinstance(ref, ObjectRef):
        return pools[ref.pool][ref.index]
    raise TypeError(f"cannot deref an un-swizzled pointer: {ref!r}")
