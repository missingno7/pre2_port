"""Pure-logic tests for the firefly simulation (pre2.recovered.firefly_sim).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_firefly_sim.py: 40 frames, 0 mismatches
on slots, both RNG seeds, scratch, and VRAM). These lock the two shared RNG generators (26CF/39DF) and the
per-slot update skeleton (dead-slot skip, timer decrement/recompute, signed-overflow move) with
hand-computed cases — the RNGs are the desync-critical part, so they get exact golden values."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.firefly_sim import (  # noqa: E402
    FireflySimState, _rng_a, _rng_b, step_fireflies)


def _dead_slots():
    # all 20 slots marked dead (first word 0x55AA) so a test can isolate one live slot
    s = bytearray(20 * 8)
    for i in range(20):
        s[i * 8] = 0xAA
        s[i * 8 + 1] = 0x55
    return s


def _state(slots=None, rng_a=0, rng_b=None):
    return FireflySimState(
        slots=bytearray(slots if slots is not None else _dead_slots()),
        rng_a=rng_a, rng_b=list(rng_b if rng_b is not None else [0, 0, 0, 0]),
        target_x=0, target_y=0, frame_gate=0, scratch=[0, 0],
        cam_col=0, cam_row=0, page=0)


def test_rng_a_lcg_golden():
    # (0 + 0x9248) ror 3 = 0x1249; returns low byte 0x49
    st = _state(rng_a=0)
    assert _rng_a(st) == 0x49
    assert st.rng_a == 0x1249
    # next step from 0x1249
    st2 = _state(rng_a=0x1249)
    expect = ((0x1249 + 0x9248) & 0xFFFF)
    expect = ((expect >> 3) | (expect << 13)) & 0xFFFF
    assert _rng_a(st2) == (expect & 0xFF)
    assert st2.rng_a == expect


def test_rng_b_golden_from_zero():
    # word=cec=ced=cee=0: word->0, cec->3, ced->3, cee->0, returns ced=3
    st = _state(rng_b=[0, 0, 0, 0])
    assert _rng_b(st) == 3
    assert st.rng_b == [0, 3, 3, 0]


def test_rng_b_carry_into_word_and_bytes():
    # word=0x00F0, cec=0x20, ced=0x05, cee=0x01
    st = _state(rng_b=[0x00F0, 0x20, 0x05, 0x01])
    out = _rng_b(st)
    word = (0x00F0 + 0x20) & 0xFFFF          # 0x0110
    dh = (word >> 8) & 0xFF                   # 0x01
    cec = (0x20 + 3 + dh) & 0xFF              # 0x24
    ced = (((0x05 + 0x01) & 0xFF) * 2 + cec) & 0xFF   # (6*2+0x24)=0x30
    cee = (0x01 ^ cec ^ ced) & 0xFF
    assert st.rng_b == [word, cec, ced, cee]
    assert out == ced


def test_dead_slot_skipped_and_not_drawn():
    st = _state()              # all 20 slots dead
    step_fireflies(st)
    assert st.slots[0] == 0xAA and st.slots[1] == 0x55   # untouched
    assert len(st.draw) == 0    # no live slots


def test_timer_decrement_no_recompute_moves_by_velocity():
    # one live slot (rest dead), timer high enough to just decrement (no recompute), velocity (vx=3,vy=-2)
    slots = _dead_slots()
    slots[0:8] = bytes([0x00, 0x10, 0x00, 0x08, 3, (256 - 2) & 0xFF, 5, 0])  # x=0x1000,y=0x0800
    st = _state(slots=slots)
    step_fireflies(st)
    assert st.slots[6] == 4                  # timer 5 -> 4 (no recompute)
    x = st.slots[0] | (st.slots[1] << 8)
    y = st.slots[2] | (st.slots[3] << 8)
    assert x == 0x1000 + 3                    # x += s8(3)
    assert y == 0x0800 - 2                    # y += s8(-2)
    assert st.draw == [(x, y, 4)]


def test_timer_underflow_triggers_recompute_consumes_rng():
    # timer 0 -> dec -> 0xFF (negative) -> recompute path draws from the RNGs
    slots = _dead_slots()
    slots[0:8] = bytes([0x00, 0x10, 0x00, 0x08, 0, 0, 0, 0])  # timer=0
    st = _state(slots=slots, rng_a=0x1234, rng_b=[1, 2, 3, 4])
    a0, b0 = st.rng_a, list(st.rng_b)
    step_fireflies(st)
    # recompute consumed rng_a (timer + vx) and rng_b (vx/vy magnitude) -> seeds advanced
    assert st.rng_a != a0
    assert st.rng_b != b0
    assert 3 <= st.slots[6] <= 10            # new timer = (rng_a & 7) + 3
