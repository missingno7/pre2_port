"""Tests for the [0x6BC5]!=0 scripted-pose / momentum FSM mode (1030:596A + cs:[0x7D6F]).

Byte-exact vs ASM proven offline against two crash-dump oracles from demo_pre2_20260626_105310
(artifacts/momentum_witness/ + scratchpad verify_momentum*.py): player_fsm_step reproduces both the
dispatch frame ([0x6BC7]&1==0) and the hold-bookkeeping frame ([0x6BC7]&1!=0), 0 real mismatches.
"""
from __future__ import annotations

from pre2.recovered.player import (
    _momentum_jump,
    player_fsm_momentum,
    player_fsm_momentum_dispatch,
)


def _mem(kv):
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    return rb, rw


def test_momentum_not_held_dispatches():
    # [0x6BC7]&1 == 0 -> arm-descent check then dispatch (do_dispatch True)
    rb, rw = _mem({0x6BC7: 0, 0x4F2A: 0x13})
    out, do_dispatch = player_fsm_momentum(rb, rw)
    assert do_dispatch is True
    assert out[0x6BC7] == 0                       # Yvel 0x13 <= 0xA0 -> not armed


def test_momentum_not_held_arms_descent_on_high_yvel():
    rb, rw = _mem({0x6BC7: 0, 0x4F2A: 0xB0})    # Yvel 0xB0 > 0xA0
    out, do_dispatch = player_fsm_momentum(rb, rw)
    assert do_dispatch is True
    assert out[0x6BC7] == 1                        # [5A06] armed


def test_momentum_hold_idle_satdecs_counter():
    # the witnessed hold frame: held ([0x6BC7]&1), no input -> sat-dec [0x6BC6], no dispatch
    rb, rw = _mem({0x6BC7: 1, 0x6BC6: 0, 0x7B1A: 3, 0x27EA: 0, 0x27EB: 0})
    out, do_dispatch = player_fsm_momentum(rb, rw)
    assert do_dispatch is False
    assert out[0x6BC7] == 1
    assert out[0x6BC6] == 0                        # sat-dec floors at 0


def test_momentum_hold_up_decrements_and_kicks_yvel():
    # held "up" (ea), [0x7B1A] high enough, [0x6BC6] nonzero -> dec counter, set Yvel = -0x40
    rb, rw = _mem({0x6BC7: 1, 0x6BC6: 5, 0x7B1A: 4, 0x27EA: 1, 0x27EB: 0, 0x4F2A: 0})
    out, do_dispatch = player_fsm_momentum(rb, rw)
    assert do_dispatch is False
    assert out[0x6BC6] == 4                        # decremented
    assert out[0x4F2A] == 0xFFC0                   # [59A9] Yvel = -0x40 (b1a >= 4)


def test_momentum_jump_runs_body_below_threshold():
    # [0x6BC8] < 0x18 -> the shared jump body runs (set_anim_b(2), arc, frictions)
    rb, rw = _mem({0x6BC8: 0, 0x6BD1: 0, 0x6BE0: 0, 0x6BC5: 1, 0x4F2A: 0, 0x4F22: 0})
    out = _momentum_jump(rb, rw)
    assert out[0x4F27] == 2                        # set_anim_b(2) -> state 2
    assert out[0x6BFE] == 0


def test_momentum_jump_ends_at_threshold():
    # [0x6BC8] >= 0x18 -> end the jump: arm descent, nudge Y, only frictions
    rb, rw = _mem({0x6BC8: 0x18, 0x4F1E: 0x200, 0x4F22: 0, 0x6BF6: 0})
    out = _momentum_jump(rb, rw)
    assert out[0x6BC7] == 1 and out[0x6BC6] == 0x18 and out[0x6BC8] == 0
    assert out[0x4F1E] == 0x1FD                    # Y -= 3
    assert 0x4F20 not in out                       # no set_anim on this path


def test_momentum_dispatch_routing():
    rb, rw = _mem({0x6BC8: 0x18, 0x4F1E: 0x200})
    # anim_id 2 -> the momentum jump; 8 -> no-op (454C ret)
    assert player_fsm_momentum_dispatch(8, rb, rw) == ({}, [])
    out2, _ = player_fsm_momentum_dispatch(2, rb, rw)
    assert out2[0x6BC7] == 1                        # routed to _momentum_jump
