"""Object-update island — unit tests for the recovered leaves.

The byte-exact ASM equivalence is proven on a live demo by pre2/probes/probe_object_tick.py (770/770 exact,
moving + static); these cover the pure formula's edge cases (sentinel, signed shift, wrap)."""
from __future__ import annotations

import pytest

from pre2.recovered.object_update import (NO_X_MOVE, AnimResult, DespawnResult, ObjectScaleUnsupported,
                                          advance_animation, apply_velocity, despawn_check, on_screen_tile)


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


# -- despawn_check (1030:8084 + 7CFF) --

def test_despawn_keep_when_state_sentinel():
    r = despawn_check(0x9999, 0x9999, state=0xFF, flags5=0, old_id=0x1234,
                      player_x=0, player_y=0, def2=0xAAAA, def4=0xFF, def7=0x5)
    assert r.kept and r == DespawnResult(True, 0x1234, 0xAAAA, 0xFF, 0x5)


def test_despawn_keep_when_drawn_bit_set():
    r = despawn_check(0x9999, 0x9999, state=1, flags5=0x20, old_id=0x10,
                      player_x=0, player_y=0, def2=0, def4=0, def7=0)
    assert r.kept


def test_despawn_keep_when_close():
    r = despawn_check(0x100, 0x100, state=1, flags5=0, old_id=0x10,   # |dx|=0x100<=0x140, |dy|=0x100<=0x12c
                      player_x=0, player_y=0, def2=0, def4=0, def7=0)
    assert r.kept and r.sprite_id == 0x10


def test_despawn_far_x_low_state_small_despawn():
    # |dx|=0x200 (>0x140) far; state<0xA -> [si+4]=0xFFFF, def4 bit2 cleared, def7=0, def2 kept
    r = despawn_check(0x200, 0, state=5, flags5=0, old_id=0x10,
                      player_x=0, player_y=0, def2=0x1111, def4=0x06, def7=0x9)
    assert r == DespawnResult(False, 0xFFFF, 0x1111, 0x02, 0)


def test_despawn_far_high_state_frees_spawn_slot():
    # far (Y), state>=0xA, def4 bit1 clear -> also free [def+2]=0xFFFF
    r = despawn_check(0, 0x200, state=0xA, flags5=0, old_id=0x10,
                      player_x=0, player_y=0, def2=0x1111, def4=0x04, def7=0)
    assert r == DespawnResult(False, 0xFFFF, 0xFFFF, 0x00, 0)


def test_despawn_far_high_state_but_def4_bit1_keeps_slot():
    r = despawn_check(0, 0x200, state=0xA, flags5=0, old_id=0x10,
                      player_x=0, player_y=0, def2=0x1111, def4=0x02, def7=0)
    assert r == DespawnResult(False, 0xFFFF, 0x1111, 0x02, 0)   # bit1 set -> spawn slot NOT freed


def test_despawn_distance_is_abs16_wrapping():
    # obj_x=0, player_x=0xFFF0 -> |0-0xFFF0| = 0x10 (NOT far on X); Y is far -> despawn
    r = despawn_check(0, 0x200, state=1, flags5=0, old_id=0,
                      player_x=0xFFF0, player_y=0, def2=0, def4=0, def7=0)
    assert not r.kept


# -- on_screen_tile (1030:8022) --

def test_onscreen_center_is_visible():
    assert on_screen_tile(0x50, 0x50, cam_x=5, cam_y=5) is True   # 0x50>>4=5 ; 5-5=0 in range


def test_onscreen_x_window_inclusive_bounds():
    assert on_screen_tile((5 + 22) * 16, 0x50, cam_x=5, cam_y=0) is True    # tx=22 (max)
    assert on_screen_tile((5 + 23) * 16, 0x50, cam_x=5, cam_y=0) is False   # tx=23 off-right
    assert on_screen_tile((5 - 2) * 16, 0x50, cam_x=5, cam_y=0) is True     # tx=-2 (min)
    assert on_screen_tile((5 - 3) * 16, 0x50, cam_x=5, cam_y=0) is False    # tx=-3 off-left


def test_onscreen_y_window_inclusive_bounds():
    assert on_screen_tile(0x50, 13 * 16, cam_x=0, cam_y=0) is True    # ty=13 (max)
    assert on_screen_tile(0x50, 14 * 16, cam_x=0, cam_y=0) is False   # ty=14 off-bottom
    assert on_screen_tile(0x50, (-3 * 16) & 0xFFFF, cam_x=0, cam_y=0) is False  # ty=-3 off-top
    assert on_screen_tile(0x50, (-2 * 16) & 0xFFFF, cam_x=0, cam_y=0) is True   # ty=-2 (min)


def test_onscreen_uses_arithmetic_shift_for_negative_pixel():
    # x = -16 (0xFFF0) >> 4 (arithmetic) = -1 ; cam 0 -> tx=-1 in [-2,22]
    assert on_screen_tile(0xFFF0, 0x50, cam_x=0, cam_y=0) is True
    # x = -48 (0xFFD0) >> 4 = -3 -> off-left
    assert on_screen_tile(0xFFD0, 0x50, cam_x=0, cam_y=0) is False
