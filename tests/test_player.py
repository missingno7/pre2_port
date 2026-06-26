"""Tests for the recovered player FSM leaves (pre2.recovered.player).

Byte-exact ASM equivalence is proven on live gameplay demos (player_x_integrate 1999/1999 on L1 + 299/299 on
L6; player_y_integrate 2069/2069 + 299/299); these pin the kinematics formulas + the boundary clamps."""
from __future__ import annotations

from pre2.recovered.player import (
    ANIM_SEQ_TABLE,
    TIMER_BYTES,
    TIMER_WORD,
    player_accel,
    player_advance_anim,
    player_friction_dir,
    player_friction_sym,
    player_gravity,
    player_set_anim,
    player_tick_timers,
    player_x_integrate,
    player_y_integrate,
)

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


def test_tick_timers_decrements_and_floors_at_zero():
    t = {a: 5 for a in TIMER_BYTES}
    t[TIMER_WORD] = 5
    out = player_tick_timers(t)
    assert all(out[a] == 4 for a in TIMER_BYTES)
    assert out[TIMER_WORD] == 4


def test_tick_timers_zero_stays_zero():
    t = {a: 0 for a in TIMER_BYTES}
    t[TIMER_WORD] = 0
    out = player_tick_timers(t)
    assert all(out[a] == 0 for a in TIMER_BYTES)   # `sub;adc` clamps at 0, not 0xFF
    assert out[TIMER_WORD] == 0


def test_accel_steps_toward_facing_and_clamps():
    # facing +1, shift 0 -> step = +0x10 ; held
    assert player_accel(0, facing=1, shift=0, input_held=True, limit=0x50) == 0x10
    # facing -1 (0xFFFF), shift 0 -> step = -0x10
    assert player_accel(0, facing=0xFFFF, shift=0, input_held=True, limit=0x50) == (-0x10) & 0xFFFF
    # clamp to +limit
    assert player_accel(0x4C, facing=1, shift=0, input_held=True, limit=0x50) == 0x50
    # clamp to -limit
    assert player_accel((-0x4C) & 0xFFFF, facing=0xFFFF, shift=0, input_held=True, limit=0x50) == (-0x50) & 0xFFFF
    # no input -> step 0, still clamps existing speed to ±limit
    assert player_accel(0x80, facing=1, shift=0, input_held=False, limit=0x50) == 0x50


def test_friction_dir_decays_and_floors():
    assert player_friction_dir(0x40, force=0x40) == 0x40 - 0x08      # -= 0x40>>3
    assert player_friction_dir((-0x5E) & 0xFFFF, force=0x40) == (-0x60) & 0xFFFF  # floor at -0x60


def test_friction_sym_pulls_toward_zero_keeping_sign():
    assert player_friction_sym(0x40, shift=0) == 0x40 - 0xC          # |v|-0xC
    assert player_friction_sym((-0x40) & 0xFFFF, shift=0) == (-(0x40 - 0xC)) & 0xFFFF
    assert player_friction_sym(0x08, shift=0) == 0                   # |v|<0xC -> 0
    assert player_friction_sym(0x40, shift=2) == 0x40 - (0xC >> 2)   # shift reduces the pull


def test_gravity_adds_and_caps_terminal():
    assert player_gravity(0x00, water=0, limit=0xC0) == 0x10         # +0x10
    assert player_gravity(0xB8, water=0, limit=0xC0) == 0xC0         # capped at terminal
    # water: gravity 4, terminal = limit>>3
    assert player_gravity(0x00, water=1, limit=0xC0) == min(4, 0xC0 >> 3)


def test_set_anim_switches_and_loads_pointer():
    table = {(0x24 + ANIM_SEQ_TABLE) & 0xFFFF: 0x9000}
    rw = lambda off: table.get(off, 0)
    # state changed -> store id, load new pointer from the seq table
    assert player_set_anim(0x12, 0x24, cur_state=0x00, cur_ptr=0x1234, read_word=rw) == (0x12, 0x9000)
    # state unchanged -> keep the running pointer (returns [0x4F28])
    assert player_set_anim(0x12, 0x24, cur_state=0x12, cur_ptr=0x1234, read_word=rw) == (0x12, 0x1234)


def test_advance_anim_frame_facing_and_pointer():
    seq = {0x9000: 0x0577}                      # frame: high 0x05, low 0x77
    rw = lambda off: seq.get(off, 0)
    # facing right (low byte 0x01 -> &0x80 == 0)
    assert player_advance_anim(0x9000, facing=0x01, read_word=rw) == (0x0577, 0x9002, 0x05)
    # facing left (low byte 0xFF -> &0x80 == 0x80) sets the high facing bit
    assert player_advance_anim(0x9000, facing=0xFF, read_word=rw) == (0x8577, 0x9002, 0x05)


def test_advance_anim_negative_word_loops_back():
    seq = {0x9000: 0xFFFC, 0x8FFC: 0x0103}     # 0x9000 holds -4 (loop marker) -> rewind to 0x8FFC
    rw = lambda off: seq.get(off, 0)
    frame, new_ptr, bcf = player_advance_anim(0x9000, facing=0x01, read_word=rw)
    assert frame == 0x0103 and new_ptr == 0x8FFE and bcf == 0x01


def test_tick_timers_byte_wraps_8bit_word_16bit():
    t = {a: 1 for a in TIMER_BYTES}
    t[TIMER_WORD] = 1
    out = player_tick_timers(t)
    assert all(out[a] == 0 for a in TIMER_BYTES)
    assert out[TIMER_WORD] == 0
    # word counter is 16-bit: 0x100 -> 0xFF (no byte wrap)
    t2 = {a: 0 for a in TIMER_BYTES}
    t2[TIMER_WORD] = 0x100
    assert player_tick_timers(t2)[TIMER_WORD] == 0xFF
