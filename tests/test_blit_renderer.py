"""Verification tests for the recovered sprite blit / bg restore (pre2.recovered.renderer).

The three blit paths (type 0 opaque copy, type 1 background restore, type ≥2 masked
composite ``(bg AND mask) OR sprite``) were proven byte-for-byte equal to the
original ASM (``1030:3B69`` dispatch + ``3D65`` bg-restore) by capturing a per-blit
witness in gameplay — the four EGA planes before and after one real blit call of
each type plus its inputs (``pre2/probes/capture_blit.py``).

The fixture below replays each captured case on a sparse plane buffer (sources
placed at their real VRAM offsets) and checks the rendered slot matches the ASM
output, locking the recovered renderer against regressions without the VM.
"""
from __future__ import annotations

import json
import pathlib

from pre2.recovered.renderer import (
    CACHE_BASE,
    ROW_STRIDE,
    ROWS,
    SLOT_BYTES,
    SPRITE_WIDTH,
    blit_sprite,
    dest_rows,
)

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "blit" / "blit_cases.json"
PLANE = 0x10000


def _build_planes(case):
    """A 4-plane buffer with the case's cache slot and background placed at their
    real offsets (everything else zero — the blit only reads those regions)."""
    planes = [bytearray(PLANE) for _ in range(4)]
    cache_off = CACHE_BASE + case["idx"] * SLOT_BYTES
    for p in range(4):
        cache = bytes.fromhex(case["cache"][p])
        planes[p][cache_off:cache_off + SLOT_BYTES] = cache
        bg = bytes.fromhex(case["bg"][p])
        for r in range(ROWS):
            for c in range(SPRITE_WIDTH):
                planes[p][(case["bg_off"] + r * ROW_STRIDE + c) & 0xFFFF] = bg[r * SPRITE_WIDTH + c]
    return planes


def _render(case):
    planes = _build_planes(case)
    mask = bytes.fromhex(case["mask"]) if "mask" in case else b""
    blit_sprite(planes, case["idx"], case["di"], case["type"], case["bg_off"], mask)
    out = []
    for p in range(4):
        out.append(bytes(planes[p][(d + c) & 0xFFFF]
                         for _r, d in dest_rows(case["di"]) for c in range(SPRITE_WIDTH)))
    return out


def _cases():
    return json.loads(FIX.read_text())


def test_blit_paths_match_asm_witness():
    cases = _cases()
    # the captured buckets cover all three dispatch paths.
    assert {cases[b]["type"] for b in cases} == {0, 1, 6} or \
        {0, 1}.issubset({min(2, cases[b]["type"]) for b in cases})
    for b, case in cases.items():
        rendered = _render(case)
        expected = [bytes.fromhex(case["expected"][p]) for p in range(4)]
        assert rendered == expected, f"blit case {b} (type {case['type']}) diverged"


def test_opaque_blit_is_a_plain_cache_copy():
    case = next(c for c in _cases().values() if c["type"] == 0)
    rendered = _render(case)
    # opaque path == the cache slot bytes, row by row, on every plane.
    for p in range(4):
        cache = bytes.fromhex(case["cache"][p])
        assert rendered[p] == cache


def test_masked_blit_composites_bg_and_mask_and_sprite():
    case = next((c for c in _cases().values() if c["type"] >= 2), None)
    assert case is not None
    mask = bytes.fromhex(case["mask"])
    # a real partial sprite must have both transparent and opaque pixels.
    assert any(mask) and any(b != 0xFF for b in mask)
    rendered = _render(case)
    # rendered == (bg AND mask) OR sprite, independently of the renderer.
    for p in range(4):
        cache = bytes.fromhex(case["cache"][p])
        bg = bytes.fromhex(case["bg"][p])
        for k in range(SLOT_BYTES):
            assert rendered[p][k] == (((bg[k] & mask[k]) | cache[k]) & 0xFF)
