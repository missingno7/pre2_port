"""Byte-exact regression for the secondary-entity update-pass leaves (1030:60DF, 581E) — see
:mod:`pre2.recovered.effects_update`.

Goldens captured from the original ASM under the VM (faithful hybrid replay of the gorilla + bonus demos, with
only these two leaves un-hooked so the ASM runs them): per active call, the DGROUP word reads the recovered
tick makes and the full list-window before/after. The recovered tick must reproduce the ASM after-window
exactly. At capture, recovered-vs-ASM was 0 mismatches over 747 (debris) + 749 (popup) live calls; these cases
are the ones where the window changed.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.effects_update import tick_debris_pool, tick_popup_ring

_FIX = Path(__file__).parent / "fixtures" / "effects_update"


def _check(name, fn, window_lo):
    cases = json.loads((_FIX / f"{name}.json").read_text())
    assert cases, f"no fixture cases for {name}"
    for case in cases:
        reads = {int(k): v for k, v in case["reads"].items()}
        before = bytes.fromhex(case["before"])
        after = bytes.fromhex(case["after"])
        writes = fn(lambda off: reads[off & 0xFFFF])
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
    _check("tick_debris_pool", tick_debris_pool, 0x5450)


def test_tick_popup_ring_byte_exact():
    _check("tick_popup_ring", tick_popup_ring, 0x4F76)
