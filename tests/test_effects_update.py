"""Byte-exact regression for the secondary-entity update-pass leaves (1030:60DF, 581E, 60FE) — see
:mod:`pre2.recovered.effects_update`.

Goldens captured from the original ASM under the VM (faithful hybrid replay of the gorilla + bonus demos, with
only these leaves un-hooked so the ASM runs them): per active call, every read the recovered tick makes
(DGROUP word ``rw``, DGROUP byte ``rb``, level-map ``tile``) and the full list-window before/after. The
recovered tick must reproduce the ASM after-window exactly. At capture, recovered-vs-ASM was 0 mismatches over
749 (popup) + 747 (debris) + 747 (particles) live calls; these cases are the ones where the window changed.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.effects_update import (tick_debris_pool, tick_particles, tick_popup_ring,
                                           tick_projectiles)

_FIX = Path(__file__).parent / "fixtures" / "effects_update"

# name -> (window_lo, call(rw, rb, tile) -> writes)
_LEAVES = {
    "tick_popup_ring": (0x4F76, lambda rw, rb, tile: tick_popup_ring(rw)),
    "tick_debris_pool": (0x5450, lambda rw, rb, tile: tick_debris_pool(rw)),
    "tick_particles": (0x50A8, lambda rw, rb, tile: tick_particles(rw, rb, tile)),
    "tick_projectiles": (0x4F2E, lambda rw, rb, tile: tick_projectiles(rw, rb)),
}


def _check(name):
    window_lo, call = _LEAVES[name]
    cases = json.loads((_FIX / f"{name}.json").read_text())
    assert cases, f"no fixture cases for {name}"
    for case in cases:
        rw_d = {int(k): v for k, v in case["rw"].items()}
        rb_d = {int(k): v for k, v in case["rb"].items()}
        tile_d = {int(k): v for k, v in case["tile"].items()}
        before = bytes.fromhex(case["before"])
        after = bytes.fromhex(case["after"])
        writes = call(lambda off: rw_d[off & 0xFFFF], lambda off: rb_d[off & 0xFFFF],
                      lambda off: tile_d[off & 0xFFFF])
        pred = bytearray(before)
        for off, (val, wid) in writes.items():
            k = (off - window_lo) & 0xFFFF
            assert 0 <= k < len(pred) and (wid == 1 or k + 1 < len(pred)), \
                f"{name}: write {off:#06x} lands outside the list window"
            pred[k] = val & 0xFF
            if wid == 2:
                pred[k + 1] = (val >> 8) & 0xFF
        assert bytes(pred) == after, f"{name}: recovered window != ASM window"


def test_tick_debris_pool_byte_exact():
    _check("tick_debris_pool")


def test_tick_popup_ring_byte_exact():
    _check("tick_popup_ring")


def test_tick_particles_byte_exact():
    _check("tick_particles")


def test_tick_projectiles_byte_exact():
    _check("tick_projectiles")
