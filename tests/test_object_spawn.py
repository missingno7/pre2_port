"""Byte-exact regression for the 6822 spawner's shared list-init 7585 (see
:mod:`pre2.recovered.object_spawn`).

``init_effect_row`` is a pure function of the caller's spawn count ``cx``; the contract is asserted directly
against the disassembly. The ASM equivalence is proven by a live shadow (0 divergences over 207 + 281 real
spawns across the gorilla + 233821 demos; the audio side-effect via 0x2CC is outside the 0x56A2 list window).
"""
from __future__ import annotations

from pre2.recovered.object_spawn import EFFECT_ROW_STRIDE, init_effect_row

_LO = 0x56A2


def _slot(off):
    return _LO + off * EFFECT_ROW_STRIDE


def test_init_effect_row_empty():
    # cx=0 -> all 8 slots are 0xFFFF terminators, no position written
    w = init_effect_row(0)
    assert all(w[_slot(i) + 4] == (0xFFFF, 2) for i in range(8))
    assert all(_slot(i) not in w for i in range(8))


def test_init_effect_row_partial():
    # cx=3 -> a 3-wide row (X = 0xD, 0x12, 0x17; Y=0xAA; id=0x135) then 5 terminators
    w = init_effect_row(3)
    for i, x in enumerate((0xD, 0x12, 0x17)):
        assert w[_slot(i) + 4] == (0x135, 2)
        assert w[_slot(i)] == (x, 2)
        assert w[_slot(i) + 2] == (0xAA, 2)
    for i in range(3, 8):
        assert w[_slot(i) + 4] == (0xFFFF, 2)


def test_init_effect_row_caps_at_8():
    w = init_effect_row(8)
    assert all(w[_slot(i) + 4] == (0x135, 2) for i in range(8))
    assert init_effect_row(20) == w        # cx>8 capped to 8
