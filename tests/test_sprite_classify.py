"""Byte-exact regression for the recovered sprite classifier (1030:4232).

Witness captured at *level load* (not a gameplay snapshot — by then the cache is
over-drawn): the four planar cache planes the classifier reads, and the resulting
256-entry sprite-type table ``[0x4DF8]`` the ASM produced. The test runs the
recovered :func:`classify_sprites` on that exact cache and asserts it reproduces the
ASM's type table byte-for-byte (all 256 slots: opaque / empty / partial-id).

The transparency masks are cross-checked against an independent witness: the
committed blit fixture (``tests/fixtures/blit/blit_cases.json``) holds a real
partial sprite's cache slot + the ASM's captured mask, so the recovered
:func:`slot_mask` is checked byte-exact against the original for that sprite.

In-VM lockstep over a real run lives in ``pre2/checkpoints/sprite_classify.py``
(verify mode); this is the fast committed check.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.sprite_classify import classify_sprites, slot_mask
from pre2.recovered.sprite_decode import NUM_SLOTS, SLOT_BYTES, SpriteCache

_FIX = Path(__file__).parent / "fixtures" / "sprite_classify"


def _witness_cache() -> SpriteCache:
    planes = [bytearray((_FIX / f"classify_cache_plane{p}.bin").read_bytes()) for p in range(4)]
    return SpriteCache(planes=planes)


def test_classify_type_table_byte_exact_vs_asm():
    cache = _witness_cache()
    expected = (_FIX / "type_table.bin").read_bytes()
    assert len(expected) == NUM_SLOTS

    result = classify_sprites(cache)
    assert len(result.types) == NUM_SLOTS
    # byte-exact over every slot (this also pins the partial-id counter sequence,
    # since each partial slot stores its running id in the type table).
    mismatches = [(i, result.types[i], expected[i]) for i in range(NUM_SLOTS)
                  if result.types[i] != expected[i]]
    assert not mismatches, f"type-table divergences: {mismatches[:8]}"

    # sanity on the witness shape (the ledger's load-time truth: 168 / 1 / 87).
    opaque = sum(1 for b in expected if b == 0)
    empty = sum(1 for b in expected if b == 1)
    partial = sum(1 for b in expected if b >= 2)
    assert (opaque, empty, partial) == (168, 1, 87)
    assert result.partial_count == partial


def test_classify_mask_byte_exact_vs_asm():
    """Cross-check the recovered transparency mask against the ASM's captured mask
    for a real partial sprite (the masked case in the blit witness)."""
    case = json.loads((Path(__file__).parent / "fixtures" / "blit" / "blit_cases.json").read_text())["2"]
    assert case["type"] >= 2, "blit case 2 should be a partial (masked) sprite"
    idx = case["idx"]
    asm_mask = bytes.fromhex(case["mask"])
    slot_planes = [bytes.fromhex(h) for h in case["cache"]]  # the slot's 4 planes (32 B each)

    cache = SpriteCache(planes=[bytearray(NUM_SLOTS * SLOT_BYTES) for _ in range(4)])
    for p in range(4):
        cache.planes[p][idx * SLOT_BYTES: idx * SLOT_BYTES + SLOT_BYTES] = slot_planes[p]

    assert slot_mask(cache, idx) == asm_mask
