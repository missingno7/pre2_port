"""The DETACHABLE pointer swizzle — the ONLY place that maps an object-model reference to/from a DGROUP offset.

``pre2/game/ref`` gives the shipped model offset-free references (``ObjectRef('actors', 3)``). This module holds
the pool region table (base + count + stride, imported from the bridge layout) and turns a reference into the
exact 16-bit offset a DOS pointer field stored, and back. The invariant ``to_offset(from_offset(v)) == v`` keeps
the serialised image byte-identical to the DOS original, so serialize->memcmp verification is preserved. Ship
without this module and the model has references and no notion of offsets. See
docs/pre2/pointer_swizzle_design.md.
"""
from __future__ import annotations

from pre2.game.ref import ObjectRef, RawRef
from pre2.bridge.game_layout import (ACTOR_BASE, ACTOR_COUNT, _BURST_BASE, _BURST_COUNT, _DEBRIS_BASE,
                                     _DEBRIS_COUNT, _DST_BASE, _DST_COUNT, _EFFECT_ROW_BASE, _EFFECT_ROW_COUNT,
                                     _PROJECTILE_BASE, _PROJECTILE_COUNT, _RING_BASE, _RING_COUNT, _SLOT0_BASE,
                                     _SLOT0_COUNT, _TARGET_BASE, _TARGET_COUNT)

_STRIDE = 0x12

#: pool name -> (base, count). Every render-slot-family pool a DOS pointer can address, stride 0x12. The offsets
#: live HERE (the bridge), never in the shipped model. Disjoint regions, so a raw offset maps to at most one.
POOL_REGIONS = {
    "slot0": (_SLOT0_BASE, _SLOT0_COUNT),
    "projectiles": (_PROJECTILE_BASE, _PROJECTILE_COUNT),
    "popup_ring": (_RING_BASE, _RING_COUNT),
    "actors": (ACTOR_BASE, ACTOR_COUNT),
    "bursts": (_BURST_BASE, _BURST_COUNT),
    "dst_pool": (_DST_BASE, _DST_COUNT),
    "debris": (_DEBRIS_BASE, _DEBRIS_COUNT),
    "target_records": (_TARGET_BASE, _TARGET_COUNT),
    "effect_row": (_EFFECT_ROW_BASE, _EFFECT_ROW_COUNT),
}


def to_offset(ref) -> int:
    """Reference -> the exact 16-bit DGROUP offset a DOS pointer field held."""
    if isinstance(ref, RawRef):
        return ref.value & 0xFFFF
    if isinstance(ref, ObjectRef):
        base, count = POOL_REGIONS[ref.pool]
        if not (0 <= ref.index < count):
            raise ValueError(f"{ref.pool}[{ref.index}] out of range (count {count})")
        return (base + ref.index * _STRIDE) & 0xFFFF
    raise TypeError(f"not a swizzlable reference: {ref!r}")


def from_offset(v: int):
    """A raw 16-bit offset -> a reference. An offset that lands on a record boundary of exactly one pool becomes
    an ``ObjectRef``; anything else (sentinels, interior, unclassified) becomes an opaque ``RawRef`` — loud,
    enumerable residue, never silently wrong."""
    v &= 0xFFFF
    for name, (base, count) in POOL_REGIONS.items():
        if base <= v < base + count * _STRIDE and (v - base) % _STRIDE == 0:
            return ObjectRef(name, (v - base) // _STRIDE)
    return RawRef(v)
