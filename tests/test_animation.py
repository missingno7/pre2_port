"""The animated-tile cycle state machine (pre2/recovered/animation.py).

``advance_animation`` is the recovered ``1030:367D..36A6`` advance, proven byte-exact vs the
ASM over 108 input cases by ``pre2/probes/verify_animation.py``. This is the committed golden
on the gate / throttle (normal & fast) / wrap logic and the frame-index derivation.
"""
from __future__ import annotations

from pre2.recovered.animation import (
    ANIM_BASE, ANIM_END, FRAME_COUNT, advance_animation, frame_index, throttle_period,
)


def test_cycle_constants_and_frame_index():
    assert (ANIM_BASE, ANIM_END, FRAME_COUNT) == (0x6688, 0x6988, 3)
    assert [frame_index(p) for p in (0x6688, 0x6788, 0x6888)] == [0, 1, 2]
    assert [throttle_period(s) for s in (0x00, 0x13, 0x14, 0x40)] == [4, 4, 2, 2]


def test_gate_inactive_freezes_cycle():
    # animated tiles absent ([0x6BBD]==0) -> neither pointer nor counter moves
    assert advance_animation(0x6788, 0x03, active=False, speed=0) == (0x6788, 0x03, False)


def test_throttle_normal():
    # mask=3 (speed<0x14): the counter advances every frame; the pointer only when (c+1)&3==0
    assert advance_animation(0x6688, 0x00, True, 0) == (0x6688, 0x01, False)
    assert advance_animation(0x6688, 0x03, True, 0) == (0x6788, 0x04, True)


def test_throttle_fast_scroll():
    # mask=1 (speed>=0x14): the pointer advances every 2nd frame
    assert advance_animation(0x6688, 0x00, True, 0x14) == (0x6688, 0x01, False)
    assert advance_animation(0x6688, 0x01, True, 0x14) == (0x6788, 0x02, True)


def test_wrap_at_end():
    # 0x6888 -> inc ah -> 0x6988 (== END) -> sub ah,3 -> 0x6688 (cycle restart)
    assert advance_animation(0x6888, 0x03, True, 0) == (0x6688, 0x04, True)
    # a full cycle returns to the start
    ptr, thr = 0x6688, 0x03
    seen = []
    for _ in range(FRAME_COUNT):
        ptr, thr, adv = advance_animation(ptr, 0x03, True, 0)
        seen.append(frame_index(ptr))
    assert seen == [1, 2, 0]   # 0x6688 -> 0x6788 -> 0x6888 -> wrap 0x6688
