"""Prehistorik 2 sprite classifier — recovered native logic (pure).

Recovers ``1030:4232`` — the load-time pass that runs right after the planar sprite
cache is filled (:func:`pre2.recovered.sprite_decode.decode_sprite_cache`, 4316 /
4389) and assigns each of the 256 cache slots a **transparency class**, saving the
partial sprites' transparency masks. The per-frame blit
(:func:`pre2.recovered.renderer.blit_sprite`) dispatches on this class and reads
these masks; until now the class/mask tables were the **only** still-ASM producer
the recovered blit depended on, so recovering this closes the renderer's sprite
pipeline end to end: **decode → classify → blit**, all recovered.

Algorithm [asm 4232..42AE]: for each 32-byte cache slot the ASM reads VRAM in EGA
**read mode 1** (colour compare, ``cmp=0``) so the result byte is ``1`` at each
pixel that is colour 0 — i.e. the per-byte transparency mask ``~(p0|p1|p2|p3)``. It
ORs the slot's 32 mask bytes into ``dh`` and ANDs them into ``dl``:

* ``dh == 0``    — no transparent pixels  → **type 0 opaque** (plain 4-plane copy);
* ``dl == 0xFF`` — every pixel transparent → **type 1 empty** (draw nothing);
* otherwise      — a **partial** sprite: take the next partial id (a counter that
  starts at 1 and pre-increments, so the first partial is ``2``) and save the 32
  mask bytes compacted at ``mask[(id-2)*0x20]`` — exactly the region the blit reads.

Pure: no ``cpu``/``mem``/``dos_re`` imports. The cache arrives as a
:class:`~pre2.recovered.sprite_decode.SpriteCache`; the VM↔memory layout (cache
planes in EGA VRAM, type table ``[0x4DF8]`` and mask region ``[0x2DF8]`` in DGROUP)
lives in ``pre2/bridge/sprites.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link
from pre2.recovered.sprite_decode import NUM_SLOTS, SLOT_BYTES, SpriteCache

__all__ = [
    "TYPE_OPAQUE", "TYPE_EMPTY", "FIRST_PARTIAL_ID",
    "ClassifyResult", "slot_mask", "classify_sprites",
]

TYPE_OPAQUE = 0          # no transparent pixels -> plain 4-plane copy
TYPE_EMPTY = 1           # fully transparent -> draw nothing (restore background only)
FIRST_PARTIAL_ID = 2     # first partial sprite's id [asm: counter seeded 1, pre-inc -> 2]


@dataclass(frozen=True)
class ClassifyResult:
    """The classifier's output — the contract the blit consumes."""
    types: bytes        # NUM_SLOTS bytes: per-slot class (0 opaque / 1 empty / >=2 partial id)
    masks: bytes        # partial masks, compacted: slot's mask at (id - FIRST_PARTIAL_ID)*SLOT_BYTES
    partial_count: int  # number of partial sprites (final counter - 1)


def slot_mask(cache: SpriteCache, slot: int) -> bytes:
    """The 32 transparency-mask bytes for one slot: ``bit=1`` where the pixel is
    colour 0 (transparent), i.e. ``~(p0|p1|p2|p3)`` per byte — the software model of
    the ASM's EGA read-mode-1 colour compare (``cmp=0``) [asm 4244..]."""
    off = slot * SLOT_BYTES
    p0, p1, p2, p3 = cache.planes
    return bytes((~(p0[off + k] | p1[off + k] | p2[off + k] | p3[off + k])) & 0xFF
                 for k in range(SLOT_BYTES))


@oracle_link("1030:4232",
             "classify each of the 256 sprite-cache slots into the type table [0x4DF8] "
             "(0 opaque / 1 empty / >=2 partial id) and save partial transparency masks "
             "compacted at [0x2DF8 + (id-2)*0x20] (the blit's mask source)",
             "ASM_MATCHED", merge_target="sprite pipeline")
def classify_sprites(cache: SpriteCache) -> ClassifyResult:
    """Recover ``1030:4232`` — assign each cache slot a transparency class + save masks."""
    types = bytearray(NUM_SLOTS)
    masks = bytearray(NUM_SLOTS * SLOT_BYTES)
    counter = 1                                   # [asm: id counter seeded 1, first partial -> 2]
    for slot in range(NUM_SLOTS):
        m = slot_mask(cache, slot)
        dh = 0           # OR of the slot's mask bytes  [asm 425C: or dh,al]
        dl = 0xFF        # AND of the slot's mask bytes  [asm 425E: and dl,al]
        for b in m:
            dh |= b
            dl &= b
        if dh == 0:                               # no transparent pixels -> opaque
            types[slot] = TYPE_OPAQUE
        elif dl == 0xFF:                          # every pixel transparent -> empty
            types[slot] = TYPE_EMPTY
        else:                                     # partial: next id, save its mask compacted
            counter += 1
            types[slot] = counter & 0xFF
            base = (counter - FIRST_PARTIAL_ID) * SLOT_BYTES
            masks[base:base + SLOT_BYTES] = m
    return ClassifyResult(types=bytes(types), masks=bytes(masks), partial_count=counter - 1)
