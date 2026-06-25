"""Object-update island — unit tests for the recovered leaves.

The byte-exact ASM equivalence is proven on a live demo by pre2/probes/probe_object_tick.py (770/770 exact,
moving + static); these cover the pure formula's edge cases (sentinel, signed shift, wrap)."""
from __future__ import annotations

from pre2.recovered.object_update import NO_X_MOVE, apply_velocity


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
