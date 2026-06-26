"""Tests for the recovered player collision leaves (pre2.recovered.player_collision).

ASM equivalence proven on live gameplay demos: collision_fall 791/791 (L1) + 138/138 (L6),
collision_hblock 98/98 (L1), collision_slope_offset 16/16 (slope demo 001513) + 8/8 (102854) -- all VERIFIED
byte-exact (the slope demo is a level with sloped/slippery tiles)."""
from __future__ import annotations

from pre2.recovered.player_collision import (
    collision_fall,
    collision_hblock,
    collision_land,
    collision_slope_offset,
)


def test_fall_sets_airborne_bit():
    assert collision_fall(0) == 1
    assert collision_fall(2) == 3          # | 1
    assert collision_fall(1) == 1          # already set


def test_hblock_undoes_x_step_and_stops():
    assert collision_hblock(0x200, 0x40) == (0x1FC, 0)               # -sar(+0x40,4) = -4
    assert collision_hblock(0x200, (-0x40) & 0xFFFF) == (0x204, 0)   # -sar(-0x40,4) = +4
    assert collision_hblock(0x200, 0x0F) == (0x200, 0)               # sar(0x0F,4)=0


def test_slope_offset_disasm_formula():
    # non-slope tile (prop & 0x30 == 0) -> returned unchanged
    assert collision_slope_offset(0x05, 9) == 0x05
    # up-slope (0x10 set): quot=(X&0xF)//3 + (prop&0xF)
    assert collision_slope_offset(0x35, 9) == 3 + 5                  # quot 9//3=3, low 5
    # down-slope (0x10 clear): (prop&0xF) - quot
    assert collision_slope_offset(0x25, 9) == 5 - 3
    # down-slope with a negative result sign-extends to a word: quot=15//3=5, low=0 -> -5
    assert collision_slope_offset(0x20, 15) == (-5) & 0xFFFF


def test_land_rising_sets_airborne():
    # Yvel < 0 (still rising) -> just set the airborne flag, no Y snap
    mem = {0x4F2A: (-0x10) & 0xFFFF, 0x6BF3: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_land(rb, rw, lambda o: 0, 0)
    assert out[0x4F24] == 0 and out[0x6BF3] == 1
    assert 0x4F1E not in out


def test_land_soft_zeroes_yvel_and_sets_flags():
    # falling onto a flat (prop 0) tile with a small fall counter -> soft land
    mem = {0x4F2A: 0x20, 0x4F1E: 0x355, 0x4F1C: 0x100, 0x6BD2: 0, 0x6BE0: 5, 0x6BCA: 0x300}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_land(rb, rw, lambda o: 0, 0x200)   # read_es -> tile 0 -> prop table[0]=0 (flat)
    assert out[0x4F1E] == 0x350          # snapped to the tile top (& 0xFFF0)
    assert out[0x4F2A] == 0              # soft land zeroes Yvel
    assert out[0x6BF3] == 2 and out[0x6BD1] == 0
    assert out[0x6BE0] == 4              # [0x6BE0] saturating-decremented 5 -> 4
    assert out[0x6BCA] == 0x350
