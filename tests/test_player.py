"""Tests for the recovered player FSM leaves (pre2.recovered.player).

Byte-exact ASM equivalence is proven on live gameplay demos (player_x_integrate 1999/1999 on L1 + 299/299 on
L6; player_y_integrate 2069/2069 + 299/299); these pin the kinematics formulas + the boundary clamps."""
from __future__ import annotations

from pre2.recovered.player import player_x_integrate, player_y_integrate

# bound = (cam_left + 0x14) << 4 ; with cam_left = 0x100 the bound is 0x1140 (> 0xFF8), so it never blocks and
# only the world-edge clamps [8, 0xFF8) apply.
_FAR = 0x100


def test_x_integrate_moves_by_signed_velocity():
    assert player_x_integrate(0x200, 0x40, cam_left=_FAR) == 0x204          # +4
    assert player_x_integrate(0x200, (-0x40) & 0xFFFF, cam_left=_FAR) == 0x1FC  # -4 (arithmetic)


def test_x_integrate_subpixel_velocity_rounds_toward_neg_inf():
    assert player_x_integrate(0x200, 0x0F, cam_left=_FAR) == 0x200          # +0
    assert player_x_integrate(0x200, (-1) & 0xFFFF, cam_left=_FAR) == 0x1FF  # floor(-1/16) = -1


def test_x_integrate_blocked_at_left_world_edge():
    assert player_x_integrate(0x0A, (-0x40) & 0xFFFF, cam_left=_FAR) == 0x0A  # 0x0A-4=6 < 8 -> stay
    assert player_x_integrate(0x0C, (-0x40) & 0xFFFF, cam_left=_FAR) == 0x08  # 0x0C-4=8 -> ok (>=8)


def test_x_integrate_blocked_at_right_world_edge():
    assert player_x_integrate(0xFF6, 0x40, cam_left=_FAR) == 0xFF6          # 0xFFA >= 0xFF8 -> blocked
    assert player_x_integrate(0xFF2, 0x40, cam_left=_FAR) == 0xFF6          # 0xFF6 < 0xFF8 -> ok


def test_x_integrate_blocked_by_camera_right_edge():
    # cam_left=0 -> bound=0x140. new_x must be < 0x140 to commit.
    assert player_x_integrate(0x138, 0x40, cam_left=0) == 0x13C             # 0x13C < 0x140 -> commit
    assert player_x_integrate(0x13E, 0x40, cam_left=0) == 0x13E             # 0x142 >= 0x140 -> blocked


def test_y_integrate_unconditional_signed_step():
    # Y += sar(Yvel,4), no clamps (collision corrects afterward)
    assert player_y_integrate(0x300, 0x80) == 0x308                        # +8 (falling)
    assert player_y_integrate(0x300, (-0x80) & 0xFFFF) == 0x2F8            # -8 (rising)
    assert player_y_integrate(0x300, (-1) & 0xFFFF) == 0x2FF              # floor(-1/16) = -1
    assert player_y_integrate(0x300, 0x0F) == 0x300                        # +0 (sub-pixel)
