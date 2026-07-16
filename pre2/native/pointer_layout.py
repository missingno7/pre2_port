"""The pointer swizzle — the ONLY place that maps an object-model reference to/from a DGROUP offset.

``pre2/game/ref`` gives the shipped model offset-free references (``ObjectRef('actors', 3)``). This module holds
the pool region table (base + count + stride, from :mod:`pre2.native.graph_layout`) and turns a reference into
the exact 16-bit offset a DOS pointer field stored, and back. The invariant ``to_offset(from_offset(v)) == v``
keeps the serialised image byte-identical to the DOS original, so serialize->memcmp verification is preserved.
See docs/pre2/pointer_swizzle_design.md.

**Why this is in ``pre2/native`` and not the bridge** (moved 2026-07-16, Stage 2.5): it is not separable from
:mod:`pre2.native.graph_layout`, which had to move so the product can build its own object graph without
importing ``pre2.bridge`` — this module imports graph_layout's pool constants, and graph_layout's
``_obj_from_image``/``_RefSwizzle`` lazily import this one. Both had to move together or neither could.
``pre2/bridge/pointer_layout.py`` re-exports these names for existing bridge-side callers.

Like graph_layout's, the offsets here are DATA consumed only at the byte boundary (image parse / materialize /
the ``rb``/``wb`` compat shim), never by gameplay logic; they retire with the last un-converted caller.
"""
from __future__ import annotations

from pre2.game.ref import AssetCursor, ObjectRef, RawRef
from pre2.native.graph_layout import (ACTOR_BASE, ACTOR_COUNT, _BURST_BASE, _BURST_COUNT, _DEBRIS_BASE,
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


#: name -> (base, end) of a read-only loaded ASSET a cursor field points INTO (any position, not a boundary).
#: The anim-script descriptor region [0xA86F, 0xB197): base is static across levels; a generous end below the
#: next fixed structure (0xB197 = the difficulty mode bytes). Values outside fall back to RawRef (byte-exact).
ASSET_REGIONS = {
    "anim_script": (0xA86F, 0xB197),    # the per-entity anim-frame descriptors (actor anim_ptr)
    "script": (0xA427, 0xA86F),         # the camera- + boss-script bytecode (cam/boss script cursors)
    "player_anim": (0x7B1B, 0x7CE0),    # the player anim/attack-script bytecode (player anim_ptr)
}


def to_offset(ref) -> int:
    """Reference -> the exact 16-bit DGROUP offset a DOS pointer field held. Tolerates a bare int (a not-yet-
    swizzled field) so the shared serialiser works whether or not the swizzle ran."""
    if isinstance(ref, int):
        return ref & 0xFFFF
    if isinstance(ref, RawRef):
        return ref.value & 0xFFFF
    if isinstance(ref, ObjectRef):
        base, count = POOL_REGIONS[ref.pool]
        if not (0 <= ref.index < count):
            raise ValueError(f"{ref.pool}[{ref.index}] out of range (count {count})")
        return (base + ref.index * _STRIDE) & 0xFFFF
    if isinstance(ref, AssetCursor):
        base, _end = ASSET_REGIONS[ref.asset]
        return (base + ref.offset) & 0xFFFF
    raise TypeError(f"not a swizzlable reference: {ref!r}")


def from_offset(v: int):
    """A raw 16-bit offset -> a reference. An offset on a pool record boundary -> ``ObjectRef``; an offset inside
    a read-only asset region -> ``AssetCursor``; anything else (sentinels, unclassified) -> an opaque ``RawRef``
    (loud, enumerable residue, never silently wrong). Regions are disjoint, so at most one classifies ``v``."""
    v &= 0xFFFF
    for name, (base, count) in POOL_REGIONS.items():
        if base <= v < base + count * _STRIDE and (v - base) % _STRIDE == 0:
            return ObjectRef(name, (v - base) // _STRIDE)
    for name, (base, end) in ASSET_REGIONS.items():
        if base <= v < end:
            return AssetCursor(name, v - base)
    return RawRef(v)
