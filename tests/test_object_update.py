"""Object-update island — unit tests for the recovered leaves.

The byte-exact ASM equivalence is proven on a live demo by pre2/probes/probe_object_tick.py (770/770 exact,
moving + static); these cover the pure formula's edge cases (sentinel, signed shift, wrap)."""
from __future__ import annotations

import pytest

from pre2.recovered.object_update import (NO_X_MOVE, AnimResult, ObjectScaleUnsupported,
                                          advance_animation, apply_velocity)


def test_positive_velocity_integrates_with_shift():
    # 0x20 (32) >> 4 = 2 px/frame
    assert apply_velocity(100, 200, 0x20, 0x40) == (102, 204)


def test_negative_velocity_is_arithmetic_shift():
    # sar(-16,4) = -1 ; sar(-1,4) = -1 (sign-preserving, toward -inf)
    assert apply_velocity(100, 200, 0xFFF0, 0xFFFF) == (99, 199)
    assert apply_velocity(100, 200, (-256) & 0xFFFF, (-256) & 0xFFFF) == (84, 184)


def test_x_sentinel_skips_x_but_still_moves_y():
    # xvel == 0xFFFF -> no X move (it is the sentinel, NOT a -1 velocity); Y still integrates
    assert apply_velocity(500, 500, NO_X_MOVE, 0x40) == (500, 504)


def test_zero_velocity_is_static():
    assert apply_velocity(123, 456, 0, 0) == (123, 456)


def test_position_wraps_mod_0x10000():
    assert apply_velocity(0xFFFF, 0xFFFF, 0x10, 0x10) == (0, 0)        # +1 each, wraps
    assert apply_velocity(0, 0, (-16) & 0xFFFF, (-16) & 0xFFFF) == (0xFFFF, 0xFFFF)  # -1 each, wraps


def test_small_subpixel_velocity_rounds_toward_neg_inf():
    # |v| < 16 -> sar gives 0 for small positive, -1 for small negative (floor)
    assert apply_velocity(50, 50, 0x0F, 0x01) == (50, 50)             # +0 each
    assert apply_velocity(50, 50, (-2) & 0xFFFF, (-15) & 0xFFFF) == (49, 49)  # floor(-2/16)=-1, floor(-15/16)=-1


def test_x_velocity_minus_one_is_unrepresentable_collides_with_sentinel():
    # A subtlety of the ASM: X-velocity -1 == 0xFFFF == the no-X-move sentinel, so the engine cannot express
    # a -1 X-velocity (it reads as "don't move X"). Y has no such sentinel. Documented, not a bug.
    assert apply_velocity(50, 50, 0xFFFF, (-15) & 0xFFFF) == (50, 49)


# -- advance_animation (1030:6881..68E6) --

def _script(words):
    """A read_word over a dict-backed script at offset 0x100 (one word per 2 bytes)."""
    mem = {0x100 + 2 * i: w & 0xFFFF for i, w in enumerate(words)}
    return 0x100, lambda off: mem.get(off & 0xFFFF, 0)


def test_anim_advances_pointer_and_sets_frame():
    ptr, rd = _script([0x10, 0x11, 0x12])
    r = advance_animation(ptr, rd, old_id=0x6005, flip_byte=0, scale=0)
    # frame = ((0x10 & 0x1FFF) + 0x138) & 0x1FFF = 0x148 ; keep old 0x6000 flags
    assert r == AnimResult(sprite_id=0x6000 | 0x148, script_ptr=0x102, attr_a340=0x00)


def test_anim_flip_sets_bit15_from_record_byte():
    ptr, rd = _script([0x10])
    r = advance_animation(ptr, rd, old_id=0x0000, flip_byte=0x80, scale=0)
    assert r.sprite_id == (0x8000 | 0x148)


def test_anim_negative_entry_is_relative_back_jump_loop():
    # entry 0 = 0x20 (frame), entry 1 = -2 (0xFFFE) loops back to entry 0. Starting AT entry 1 -> back to 0.
    ptr, rd = _script([0x20, (-2) & 0xFFFF])
    r = advance_animation(0x102, rd, old_id=0, flip_byte=0, scale=0)   # start on the back-jump word
    assert r.sprite_id == ((0x20 + 0x138) & 0x1FFF)
    assert r.script_ptr == 0x102        # jumped back to 0x100, consumed -> +2 = 0x102


def test_anim_a340_takes_flag_bits_of_raw_frame():
    # a340 = (raw>>8)&0xE0 ; a valid (non-negative) frame entry has bit15=0, so only bits 13,14 (0x6000) show.
    ptr, rd = _script([0x6000 | 0x10])
    r = advance_animation(ptr, rd, old_id=0, flip_byte=0, scale=0)
    assert r.attr_a340 == 0x60           # (0x6010>>8)&0xE0 | scale(0)


def test_anim_scale_active_is_unsupported_guard():
    ptr, rd = _script([0x10])
    with pytest.raises(ObjectScaleUnsupported):
        advance_animation(ptr, rd, old_id=0, flip_byte=0, scale=7)


def test_anim_runaway_backjump_raises():
    # a reader that always returns a negative word -> the back-jump never terminates -> guard raises
    with pytest.raises(ObjectScaleUnsupported):
        advance_animation(0x100, lambda off: 0xFFFE, old_id=0, flip_byte=0, scale=0)
