"""Tests for the recovered player collision leaves (pre2.recovered.player_collision).

ASM equivalence proven on live gameplay demos: collision_fall 791/791 (L1) + 138/138 (L6),
collision_hblock 98/98 (L1). collision_slope_offset is unwitnessed in the L1/L6 corpus (no slope tiles), so
its test pins the recovered formula from the disasm (ASM_MATCHED, not yet lockstep-VERIFIED)."""
from __future__ import annotations

from pre2.recovered.player_collision import collision_fall, collision_hblock, collision_slope_offset


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
