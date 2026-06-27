"""Tests for the effect-sprite projector (1030:8922 -> project_particles).

Byte-exact vs ASM proven in shadow (456 calls, 0 mismatches across six demos; projection + bounce paths
exercised — scratchpad shadow_8922.py). These guard the recovered logic in the committed suite.
"""
from __future__ import annotations

from pre2.recovered.object_particles import (
    CAM_X,
    CAM_Y,
    DST_SLOTS,
    FREEZE_FLAG,
    SRC_LIST,
    project_particles,
)


def _mem(kv):
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    return rb, rw


def _entry(kv, idx, x, y, sprite, counter=0):
    si = SRC_LIST + idx * 7
    kv[si] = x
    kv[si + 2] = y
    kv[si + 4] = sprite
    kv[si + 6] = counter
    return si


def test_all_empty_clears_every_slot():
    # every source sprite id == 0xFFFF -> nothing projected, all 20 dest slots cleared
    kv = {}
    for i in range(0x46):
        kv[SRC_LIST + i * 7 + 4] = 0xFFFF
    out = project_particles(*_mem(kv))
    cleared = [off for off, (v, w) in out.items() if v == 0xFFFF and ((off - DST_SLOTS) % 0x12) == 4]
    assert len(cleared) == 0x14
    assert all(v == 0xFFFF for off, (v, w) in out.items() if off >= DST_SLOTS)


def test_onscreen_entry_projects_into_first_slot():
    kv = {CAM_X: 0x10, CAM_Y: 0x08}
    # world X>>4 = 0x14, minus cam 0x10 = 4 (in [0,0x16]); Y>>4 = 0x10 - 8 = 8 (in [0,0x2b])
    si = _entry(kv, 0, x=0x140, y=0x100, sprite=0x07)
    for i in range(1, 0x46):
        kv[SRC_LIST + i * 7 + 4] = 0xFFFF
    out = project_particles(*_mem(kv))
    assert out[DST_SLOTS] == (0x140, 2)          # raw X copied
    assert out[DST_SLOTS + 4] == (0x07, 2)       # sprite id
    assert out[DST_SLOTS + 9] == (si, 2)         # back-reference
    # next slot (the second) is cleared since only one entry was on-screen
    assert out[DST_SLOTS + 0x12 + 4] == (0xFFFF, 2)


def test_offscreen_entries_are_culled():
    kv = {CAM_X: 0x40, CAM_Y: 0x08}
    _entry(kv, 0, x=0x000, y=0x100, sprite=0x01)   # X>>4=0 < cam 0x40 -> jb cull
    _entry(kv, 1, x=0x900, y=0x100, sprite=0x02)   # X>>4=0x90-0x40=0x50 > 0x16 -> jg cull
    for i in range(2, 0x46):
        kv[SRC_LIST + i * 7 + 4] = 0xFFFF
    out = project_particles(*_mem(kv))
    # nothing projected -> first slot cleared, no source bounce writes
    assert out[DST_SLOTS + 4] == (0xFFFF, 2)
    assert (SRC_LIST + 6) not in out and (SRC_LIST + 7 + 6) not in out


def test_bounce_counter_pingpongs_and_advances_y():
    kv = {CAM_X: 0, CAM_Y: 0}
    si = _entry(kv, 0, x=0x010, y=0x100, sprite=0x05, counter=3)  # counter 3 -> +1 ->4 -> trigger neg
    for i in range(1, 0x46):
        kv[SRC_LIST + i * 7 + 4] = 0xFFFF
    out = project_particles(*_mem(kv))
    # cbw(3)+1 = 4 ; al=4 not < 4 -> neg+inc: ax = -4+1 = -3 -> stored 0xFD, Y += -3
    assert out[si + 6] == (0xFD, 1)
    assert out[si + 2] == ((0x100 - 3) & 0xFFFF, 2)
    assert out[DST_SLOTS + 2] == ((0x100 - 3) & 0xFFFF, 2)


def test_freeze_flag_skips_animation():
    kv = {CAM_X: 0, CAM_Y: 0, FREEZE_FLAG: 1}
    si = _entry(kv, 0, x=0x010, y=0x100, sprite=0x05, counter=2)
    for i in range(1, 0x46):
        kv[SRC_LIST + i * 7 + 4] = 0xFFFF
    out = project_particles(*_mem(kv))
    assert (si + 6) not in out                    # counter untouched
    assert out[si + 2] == (0x100, 2)              # Y unchanged (ax == 0)
    assert out[DST_SLOTS + 2] == (0x100, 2)


def test_more_than_20_onscreen_fills_all_slots_without_clear():
    # 21 on-screen entries -> first 20 fill every slot; bx hits 0 -> no tail-clear pass
    kv = {CAM_X: 0, CAM_Y: 0}
    for i in range(21):
        _entry(kv, i, x=0x010 + (i << 4), y=0x100, sprite=0x05)
    for i in range(21, 0x46):
        kv[SRC_LIST + i * 7 + 4] = 0xFFFF
    out = project_particles(*_mem(kv))
    dst_end = DST_SLOTS + 0x14 * 0x12
    filled = [off for off, (v, w) in out.items()
              if DST_SLOTS <= off < dst_end and ((off - DST_SLOTS) % 0x12) == 9]  # back-refs written
    assert len(filled) == 0x14                     # all 20 slots filled
    assert not any(v == 0xFFFF for off, (v, w) in out.items()
                   if DST_SLOTS <= off < dst_end)  # no clear pass
