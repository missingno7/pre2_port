"""Object-update island — unit tests for the recovered leaves.

The byte-exact ASM equivalence is proven on a live demo by pre2/probes/probe_object_tick.py (770/770 exact,
moving + static); these cover the pure formula's edge cases (sentinel, signed shift, wrap)."""
from __future__ import annotations

import pytest

from pre2.recovered.object_update import (NO_X_MOVE, AnimResult, DespawnResult, ObjectScaleUnsupported,
                                          advance_animation, anim_script_forward, anim_script_rewind,
                                          apply_velocity, despawn_check, dying_state, handle_object_7665,
                                          handle_object_773d, handle_object_77de, handle_object_7c8c,
                                          handle_object_7c90, handle_object_760f, handle_object_7c2d,
                                          handle_object_7b91, handle_object_7adf, orbit_position,
                                          handle_object_7898, handle_object_75c4, handle_object_78ec, handle_object_7a60,
                                          on_screen_tile, saturating_counter, spawn_effects)


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


# -- anim_script_rewind / anim_script_forward (1030:8048 / 8058) --

def test_anim_rewind_stops_at_negative_marker():
    mem = {0x100: (-4) & 0xFFFF, 0x102: 0x10, 0x104: 0x11, 0x106: 0x12}
    rd = lambda o: mem.get(o & 0xFFFF, 0)
    assert anim_script_rewind(0x106, rd) == 0x100   # back over 0x104,0x102 (>=0), stop at the negative 0x100


def test_anim_forward_steps_past_negative_marker():
    mem = {0x100: 0x10, 0x102: 0x11, 0x104: (-2) & 0xFFFF}
    rd = lambda o: mem.get(o & 0xFFFF, 0)
    assert anim_script_forward(0x100, rd) == 0x106  # 0x100,0x102 >=0, 0x104<0 -> step +2 past it


def test_anim_seek_runaway_raises():
    with pytest.raises(ObjectScaleUnsupported):
        anim_script_rewind(0x100, lambda o: 0x10)   # never negative -> runaway guard
    with pytest.raises(ObjectScaleUnsupported):
        anim_script_forward(0x100, lambda o: 0x10)


# -- handle_object_7665 (idx10 AI state machine, 1030:7665..773C) --

def _o(**kw):
    o = dict(x=0x100, y=0x100, id=0x0187, xvel=0, yvel=0, anim_ptr=0x200, state=0)
    o.update(kw); return o

def _d(**kw):
    d = dict(d2=0x1111, d4=0, d7=0, dD=4)
    d.update(kw); return d

def _g(**kw):
    g = dict(mode=0, shake=0, a340=0, frame=1, player_x=0x100, player_y=0x100)
    g.update(kw); return g

_RD = lambda off: 0xFFFE   # script reader: immediate back-jump -> anim_script_forward returns ptr+2


def test_h7665_state0_settles_to_arm_when_stopped():
    o, d = _o(state=0, yvel=0), _d()
    handle_object_7665(o, d, _g(), _RD)
    assert o["state"] == 1 and (d["d4"] & 0x18) == 0x18


def test_h7665_state0_stays_while_falling():
    o, d = _o(state=0, yvel=0x40), _d()
    handle_object_7665(o, d, _g(), _RD)
    assert o["state"] == 0


def test_h7665_state1_arms_timer_and_advances_anim():
    o, d = _o(state=1, yvel=0, anim_ptr=0x200), _d()
    handle_object_7665(o, d, _g(), _RD)
    assert o["state"] == 2 and d["d7"] == 0x1E and o["anim_ptr"] == 0x202


def test_h7665_state1_falls_back_when_moving():
    o, d = _o(state=1, yvel=5), _d()
    handle_object_7665(o, d, _g(), _RD)
    assert o["state"] == 0


def test_h7665_state2_charges_toward_player_each_side():
    o, d = _o(state=2, xvel=0, x=0x100), _d(dD=4, d7=5)
    handle_object_7665(o, d, _g(a340=1, player_x=0x180, frame=1), _RD)   # objX<playerX (close) -> +dD
    assert o["xvel"] == 4 and d["d4"] == 0x0F and d["d7"] == 5           # frame&3 -> no timer dec
    o, d = _o(state=2, xvel=0, x=0x180), _d(dD=4, d7=5)
    handle_object_7665(o, d, _g(a340=1, player_x=0x100, frame=1), _RD)   # objX>=playerX -> -dD
    assert o["xvel"] == (-4) & 0xFFFF


def test_h7665_state2_idle_when_not_anim_ready():
    o, d = _o(state=2, xvel=0), _d(d7=5)
    handle_object_7665(o, d, _g(a340=0, frame=4), _RD)                   # |xvel|<0x10 & a340==0 -> early ret
    assert o["state"] == 2 and d["d7"] == 5


def test_h7665_state2_timer_expires_to_state3():
    o, d = _o(state=2, xvel=0x20, anim_ptr=0x200), _d(d7=1)              # |xvel|>=0x10 skips charge
    handle_object_7665(o, d, _g(frame=4), _RD)                          # frame&3==0 -> dec d7 1->0 -> state 3
    assert o["state"] == 3 and o["yvel"] == 0 and d["d4"] == 0x36 and o["anim_ptr"] == 0x202


def test_h7665_state3_despawns_when_anim_done():
    o, d = _o(state=3, id=0x187), _d(d4=0)
    handle_object_7665(o, d, _g(a340=1), _RD)
    assert o["id"] == 0xFFFF


def test_h7665_state_ff_despawns_when_flag_clear():
    o, d = _o(state=0xFF, id=0x187), _d(d4=0)                           # def4 bit0 clear -> despawn
    handle_object_7665(o, d, _g(), _RD)
    assert o["id"] == 0xFFFF


def test_h7665_state_ff_applies_gravity_when_drawn():
    o, d = _o(state=0xFF, id=0x2187, yvel=0), _d(d4=1)                  # id bit13 (drawn) + def4 bit0 -> gravity
    handle_object_7665(o, d, _g(), _RD)
    assert o["yvel"] == 0xF and o["id"] == 0x2187


# -- handle_object_773d (idx9 horizontal-patrol enemy, 1030:773D) --

def _o9(**kw):
    o = dict(x=0x200, y=0x100, id=0x2000 | 0x14F, xvel=0, yvel=0, state=0)   # id bit13 set = drawn
    o.update(kw); return o

def _d9(**kw):
    left = kw.pop("dD", 0x100)        # patrol bounds are 16-bit but stored as byte-union halves (dD/dE, dF/d10)
    right = kw.pop("dF", 0x300)
    d = dict(d2=0x1111, d4=1, d7=0, dD=left & 0xFF, dE=(left >> 8) & 0xFF,
             dF=right & 0xFF, d10=(right >> 8) & 0xFF, d11=0, d12=20)
    d.update(kw); return d

def _g9(**kw):
    g = dict(player_x=0x200, player_y=0x100)
    g.update(kw); return g


def test_h773d_state0_patrols_right_and_accelerates():
    o, d = _o9(state=0, x=0x200), _d9(d11=0, d12=20, dF=0x300)
    handle_object_773d(o, d, _g9())
    assert o["xvel"] == 0 and d["d11"] == 3 and o["state"] == 0


def test_h773d_state0_speed_caps_at_d12():
    o, d = _o9(state=0, x=0x200), _d9(d11=19, d12=20, dF=0x300)   # 19+3=22 > 20 -> no store
    handle_object_773d(o, d, _g9())
    assert d["d11"] == 19 and o["xvel"] == 19


def test_h773d_state0_turns_at_right_bound():
    o, d = _o9(state=0, x=0x300), _d9(dF=0x2FF)                    # dF < x -> turn to state 1
    handle_object_773d(o, d, _g9())
    assert o["state"] == 1


def test_h773d_state1_patrols_left_and_turns():
    o, d = _o9(state=1, x=0x200), _d9(d11=5, d12=20, dD=0x100)     # dD < x -> no turn
    handle_object_773d(o, d, _g9())
    assert o["xvel"] == 5 and d["d11"] == 2 and o["state"] == 1
    o, d = _o9(state=1, x=0x100), _d9(dD=0x100)                    # dD >= x -> turn to state 0
    handle_object_773d(o, d, _g9())
    assert o["state"] == 0


def test_h773d_despawns_when_player_too_far_vertically():
    o, d = _o9(id=0x14F, y=0x100, state=0), _d9(d4=5)              # not drawn (no bit13)
    handle_object_773d(o, d, _g9(player_y=0x100 + 0xBE))          # |dY|>=0xBE -> despawn
    assert o["id"] == 0xFFFF and (d["d4"] & 4) == 0               # [def+4] bit2 cleared


def test_h773d_drawn_object_skips_despawn():
    o, d = _o9(id=0x2000 | 0x14F, y=0x100, state=0), _d9()
    handle_object_773d(o, d, _g9(player_y=0xFFFF))               # would be far, but drawn -> no despawn
    assert o["id"] == (0x2000 | 0x14F)


def test_dying_state_gravity_vs_despawn():
    o, d = dict(id=0x2000, yvel=0), dict(d2=0, d4=1, d7=0)       # held + drawn -> gravity
    dying_state(o, d, dict(player_y=0, player_x=0))
    assert o["yvel"] == 0xF
    o, d = dict(id=0, yvel=0), dict(d2=0, d4=0, d7=0)            # def4 bit0 clear -> despawn
    dying_state(o, d, dict(player_y=0, player_x=0))
    assert o["id"] == 0xFFFF


# -- saturating_counter (8001), handle_object_7c8c (idx1), handle_object_77de (idx8 pouncer) --

def test_saturating_counter():
    assert saturating_counter(2, 0) == (1, False)        # 1>>2=0 < 2
    assert saturating_counter(2, 7) == (8, True)         # 8>>2=2 >= 2
    assert saturating_counter(0, 0xFF) == (0xFF, True)   # saturates at 0xFF


def test_h7c8c_is_despawn_only():
    o, d = dict(x=0x500, y=0, id=0x10, state=0), dict(d2=0, d4=0, d7=0)   # far -> despawn
    handle_object_7c8c(o, d, dict(player_x=0, player_y=0))
    assert o["id"] == 0xFFFF
    o, d = dict(x=0, y=0, id=0x10, state=0), dict(d2=0, d4=0, d7=0)       # near -> keep
    handle_object_7c8c(o, d, dict(player_x=0, player_y=0))
    assert o["id"] == 0x10


_RD8 = lambda off: 0xFFFE   # script reader: forward -> +2, rewind -> -2

def _o8(**kw):
    o = dict(x=0x200, y=0x100, id=0x2000 | 0x18C, xvel=5, yvel=0, anim_ptr=0x200, state=0)  # bit13 = drawn
    o.update(kw); return o

def _d8(**kw):
    d = dict(d2=0, d4=0, d6=2, d7=0, dD=8, dE=3, dF=2, d10=4, d11=0, d12=0)
    d.update(kw); return d

def _g8(**kw):
    g = dict(player_x=0x208, player_y=0x100)
    g.update(kw); return g


def test_h77de_state0_waits_until_counter_ready():
    o, d = _o8(state=0), _d8(d6=5, d7=0)                  # 1>>2=0 < 5 -> not ready
    handle_object_77de(o, d, _g8(), _RD8)
    assert d["d7"] == 1 and o["state"] == 0


def test_h77de_state0_pounces_when_ready_and_in_range():
    o, d = _o8(state=0, x=0x200, y=0x100, anim_ptr=0x200), _d8(d6=0, dD=8, dE=3, dF=2, d10=4)
    handle_object_77de(o, d, _g8(player_x=0x208, player_y=0x100), _RD8)  # in range, ready -> pounce
    assert o["state"] == 0xA
    assert o["yvel"] == (-(3 << 4)) & 0xFFFF             # -([def+0xE]<<4)
    assert o["xvel"] == (2 << 4)                         # toward player (playerX > objX)
    assert o["anim_ptr"] == 0x202 and d["d4"] == 0x2C


def test_h77de_state0_no_pounce_when_out_of_range():
    o, d = _o8(state=0), _d8(d6=0, dD=1, d10=1)          # ready but player far in tiles
    handle_object_77de(o, d, _g8(player_x=0x400, player_y=0x100), _RD8)
    assert o["state"] == 0


def test_h77de_rise_fall_land_cycle():
    o, d = _o8(state=0xA, yvel=(-5) & 0xFFFF), _d8()     # rising
    handle_object_77de(o, d, _g8(), _RD8)
    assert o["state"] == 0xA
    o, d = _o8(state=0xA, yvel=2, anim_ptr=0x200), _d8()  # apex -> falling
    handle_object_77de(o, d, _g8(), _RD8)
    assert o["state"] == 0xB and o["anim_ptr"] == 0x202
    o, d = _o8(state=0xB, yvel=0, xvel=9, anim_ptr=0x208), _d8(d7=99)   # landed
    handle_object_77de(o, d, _g8(), _RD8)
    assert o["state"] == 0xC and o["xvel"] == 0 and d["d7"] == 0 and o["anim_ptr"] == 0x204


def test_h77de_faces_player_when_stationary():
    o, d = _o8(state=0xFF, xvel=0, x=0x200, id=0x2000 | 0x18C), _d8(d4=1)
    handle_object_77de(o, d, _g8(player_x=0x300), _RD8)   # xvel==0 -> face right (objX<playerX) -> +1
    assert o["xvel"] == 1


# -- handle_object_7c90 (idx0 ground enemy/collectible, 1030:7C90) --

def _o0(**kw):
    o = dict(x=0x100, y=0x100, id=0x2000 | 0x160, xvel=0, yvel=0, anim_ptr=0x200, state=0)
    o.update(kw); return o

def _d0(**kw):
    d = dict(d2=0, d4=0, d6=0, d7=0)
    d.update(kw); return d

_RD0 = lambda off: 0xFFFE


def test_h7c90_state0_activates_near_player():
    o, d = _o0(state=0), _d0(d6=0)
    handle_object_7c90(o, d, dict(player_x=0x100, player_y=0x100), _RD0)
    assert (d["d4"] & 8) == 8 and o["state"] == 1


def test_h7c90_state0_waits_until_ready():
    o, d = _o0(state=0), _d0(d6=5, d7=0)                              # 1>>2=0 < 5 -> not ready
    handle_object_7c90(o, d, dict(player_x=0x100, player_y=0x100), _RD0)
    assert o["state"] == 0 and (d["d4"] & 8) == 0


def test_h7c90_state0_despawns_when_far_below_player():
    o, d = _o0(state=0, y=0x200), _d0(d6=0, d4=1)                     # objY-playerY = 0x100 >= 0xB0
    handle_object_7c90(o, d, dict(player_x=0x200, player_y=0x100), _RD0)
    assert o["id"] == 0xFFFF


def test_h7c90_state1_chases_once_landed():
    o, d = _o0(state=1, yvel=0, x=0x100, anim_ptr=0x200), _d0()
    handle_object_7c90(o, d, dict(player_x=0x200, player_y=0x100), _RD0)
    assert o["state"] == 2 and o["xvel"] == 0x20 and o["anim_ptr"] == 0x202


def test_h7c90_state1_waits_while_falling():
    o, d = _o0(state=1, yvel=5), _d0()
    handle_object_7c90(o, d, dict(player_x=0x100, player_y=0x100), _RD0)
    assert o["state"] == 1


# -- handle_object_760f (idx11 leaping squirrel, 1030:760F) --

def _o11(**kw):
    o = dict(x=0x100, y=0x100, id=0x2000 | 0x16B, xvel=0, yvel=0, state=0)
    o.update(kw); return o

def _d11(**kw):
    d = dict(d2=0, d4=0, d7=0, dD=4, dE=6, dF=8)
    d.update(kw); return d

def _g11(**kw):
    g = dict(a340=1, player_x=0x100, player_y=0x100)
    g.update(kw); return g


def test_h760f_state0_waits_for_anim_ready():
    o, d = _o11(state=0), _d11()
    handle_object_760f(o, d, _g11(a340=0), None)
    assert o["state"] == 0


def test_h760f_state0_leaps_toward_player():
    o, d = _o11(state=0, x=0x100), _d11(dD=4, dE=6)
    handle_object_760f(o, d, _g11(a340=1, player_x=0x200), None)
    assert o["state"] == 1
    assert o["xvel"] == (4 << 4)                 # leap toward player (playerX > objX)
    assert o["yvel"] == (-(6 << 4)) & 0xFFFF     # jump up


def test_h760f_state1_gravity_until_terminal():
    o, d = _o11(state=1, yvel=0), _d11(dF=8)      # terminal = 8<<4 = 0x80
    handle_object_760f(o, d, _g11(), None)
    assert o["yvel"] == 8
    o, d = _o11(state=1, yvel=0x80), _d11(dF=8)   # at terminal -> no more accel
    handle_object_760f(o, d, _g11(), None)
    assert o["yvel"] == 0x80


# -- handle_object_7c2d (idx2 vertical-bob, 1030:7C2D) + spawn_effects (7FD9) --

def _o2(**kw):
    o = dict(x=0x100, y=0x110, id=0x2000 | 0x13E, xvel=0, yvel=0, anim_ptr=0x200, state=0)
    o.update(kw); return o

def _d2(**kw):
    d = dict(d2=0, d4=0, d7=0, d9=0x100, dB=0x100, dD=8, dE=2)
    d.update(kw); return d

def _g2(**kw):
    g = dict(player_x=0x100, player_y=0x110)
    g.update(kw); return g

_RD2 = lambda off: 0xFFFE


def test_h7c2d_state0_moves_down_within_amplitude():
    o, d = _o2(y=0x102, state=0), _d2(dB=0x100, dD=8, dE=2)   # rel_y=2 ; _s8(8) >= _s8(2) -> stay down
    handle_object_7c2d(o, d, _g2(player_y=0x102), _RD2)
    assert o["yvel"] == (2 << 4) and o["state"] == 0


def test_h7c2d_state0_turns_up_at_amplitude():
    o, d = _o2(y=0x120, state=0, anim_ptr=0x200), _d2(dB=0x100, dD=8)   # rel_y=0x20 > 8 -> turn up
    handle_object_7c2d(o, d, _g2(player_y=0x120), _RD2)
    assert o["state"] == 1 and o["anim_ptr"] == 0x202


def test_h7c2d_state1_moves_up_then_returns_at_centre():
    o, d = _o2(y=0x110, state=1), _d2(dB=0x100, dE=2)         # rel_y=0x10 >= 0 -> keep rising
    handle_object_7c2d(o, d, _g2(player_y=0x110), _RD2)
    assert o["yvel"] == (-(2 << 4)) & 0xFFFF and o["state"] == 1
    o, d = _o2(y=0x0F0, state=1, anim_ptr=0x200), _d2(dB=0x100)   # rel_y=-0x10 < 0 -> turn down
    handle_object_7c2d(o, d, _g2(player_y=0x0F0), _RD2)
    assert o["state"] == 0 and o["anim_ptr"] == 0x1FE


def test_spawn_effects_three_entries():
    free = [[0xFFFF, 0, 0, 0] for _ in range(5)]
    it = iter(free)
    out = spawn_effects(def9=0x123, defB=0x200, arg=0, dl=8, find_free=lambda: next(it))   # step=2, y=0x1E8
    assert out == [(0x123, 0x1E8, 0, 2), (0x123, 0x1E8, 0, 4), (0x123, 0x1E8, 0, 6)]
    assert free[0] == [0x123, 0x1E8, 0, 2] and free[2] == [0x123, 0x1E8, 0, 6]


# -- handle_object_7b91 (idx3 falling/landing enemy, 1030:7B91) --

def _o3(**kw):
    o = dict(x=0x100, y=0x100, id=0x2000 | 0x144, xvel=0, yvel=0, anim_ptr=0x200, state=0)
    o.update(kw); return o

def _d3(**kw):
    d = dict(d2=0, d4=0, d6=0, d7=0, d9=0x100, dB=0x100, dD=8)
    d.update(kw); return d

def _g3(**kw):
    g = dict(player_x=0x100, player_y=0x100)
    g.update(kw); return g

_RD3 = lambda off: 0xFFFE


def test_h7b91_state0_waits_until_player_near():
    o, d = _o3(state=0), _d3(d6=0, d9=0x100, dD=2)
    handle_object_7b91(o, d, _g3(player_x=0x300), _RD3, tile_prop=lambda x, y: 0)   # distX=0x20 > 2 -> wait
    assert o["state"] == 0


def test_h7b91_state0_falls_when_player_near():
    o, d = _o3(state=0), _d3(d6=0, d9=0x100, dD=8)
    handle_object_7b91(o, d, _g3(player_x=0x100), _RD3, tile_prop=lambda x, y: 0)   # distX=0 <= 8 -> fall
    assert o["state"] == 1 and o["yvel"] == 0x20 and o["anim_ptr"] == 0x202


def test_h7b91_state1_falls_until_solid_then_lands():
    o, d = _o3(state=1, y=0x105, anim_ptr=0x200), _d3()
    handle_object_7b91(o, d, _g3(), _RD3, tile_prop=lambda x, y: 0)                 # no ground -> keep falling
    assert o["state"] == 1 and o["y"] == 0x105
    o, d = _o3(state=1, y=0x105, anim_ptr=0x200), _d3()
    handle_object_7b91(o, d, _g3(player_x=0x200), _RD3, tile_prop=lambda x, y: 1)   # solid -> land
    assert o["state"] == 2 and o["y"] == 0x100 and o["yvel"] == 0
    assert (d["d4"] & 0x48) == 0x48 and o["xvel"] == 0x30


def test_h7b91_state2_bounces_off_left_edge():
    o, d = _o3(state=2, x=(-1) & 0xFFFF, xvel=0x30), _d3()
    handle_object_7b91(o, d, _g3(player_x=0), _RD3, tile_prop=lambda x, y: 0)
    assert o["xvel"] == (-0x30) & 0xFFFF


# -- orbit_position (7B53) + handle_object_7adf (idx4 orbit/pendulum enemy, 1030:7ADF) --

def test_orbit_position_math():
    # X = centreX + ((s8(cos)>>2)*s8(radius))>>4 ; Y likewise with sin.
    assert orbit_position(0x100, 0x80, 0x10, 0x40, 0x00) == ((0x100 + (((0x40 >> 2) * 0x10) >> 4)) & 0xFFFF, 0x80)
    # negative cos (signed byte) descends X via arithmetic shift
    cx = (0x100 + (((_neg8(0x80) >> 2) * 0x10) >> 4)) & 0xFFFF
    assert orbit_position(0x100, 0x80, 0x10, 0x80, 0x00)[0] == cx


def _neg8(v):
    return v - 0x100 if v & 0x80 else v


def _o4(**kw):
    o = dict(x=0x100, y=0x80, id=0x14F, state=0); o.update(kw); return o

def _d4(**kw):
    d = dict(d2=0x1111, d4=1, d7=0, d6=0, d9=0x100, dB=0x100, dD=0x20, dE=0x40, dF=0, d10=0)
    d.update(kw); return d

def _g4(**kw):
    g = dict(player_x=0x100, player_y=0x100); g.update(kw); return g

_COS = lambda a: 0x40   # constant tables -> deterministic orbit position
_SIN = lambda a: 0x00


def test_h7adf_state0_descends_until_depth():
    o, d = _o4(state=0, y=0x100), _d4(d6=0, dB=0x100, dD=0x20)   # relY=0 < radius 0x20 -> descend
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert o["y"] == 0x102 and o["state"] == 0


def test_h7adf_state0_reaches_orbit():
    o, d = _o4(state=0, y=0x130), _d4(d6=0, dB=0x100, dD=0x20)   # relY=0x30 >= radius 0x20 -> state 1
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert o["state"] == 1


def test_h7adf_state0_counter_gates_descent():
    o, d = _o4(state=0, y=0x100), _d4(d6=5, d7=0)               # counter not ready -> no move
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert o["y"] == 0x100 and d["d7"] == 1


def test_h7adf_state1_spins_angle_and_clamps():
    o, d = _o4(state=1), _d4(dF=0, dE=0x40, d9=0x100, dB=0x100, dD=0x20)
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert d["dF"] == 4 and o["state"] == 1                      # angle 0->4, below max 0x40
    o, d = _o4(state=1), _d4(dF=0x3E, dE=0x40)                   # 0x3E+4=0x42 >= 0x40 -> clamp + state 2
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert d["dF"] == 0x40 and o["state"] == 2


def test_h7adf_state2_pendulum():
    o, d = _o4(state=2), _d4(dF=0x10, d10=0)                     # angle 0x10 >= 0 -> dl=-1
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert d["d10"] == 0xFF and d["dF"] == (0x10 + _neg8(0xFF)) & 0xFF   # d10=-1, angle += -1


def test_h7adf_state2_sets_position_on_orbit():
    o, d = _o4(state=2, x=0, y=0), _d4(dF=0x10, d10=0, d9=0x100, dB=0x80, dD=0x10)
    handle_object_7adf(o, d, _g4(), cos_table=_COS, sin_table=_SIN)
    assert (o["x"], o["y"]) == orbit_position(0x100, 0x80, 0x10, 0x40, 0x00)


# -- handle_object_7898 (idx7 creeper/leaper, 1030:7898) --

def _o7(**kw):
    o = dict(x=0x200, y=0x100, id=0x14F, xvel=0, yvel=0, state=0, anim_ptr=0x100); o.update(kw); return o

def _d7(**kw):
    d = dict(d2=0x1111, d4=1, d7=0, dD=2, dE=5); d.update(kw); return d

def _g7(**kw):
    g = dict(player_x=0x200, player_y=0x100); g.update(kw); return g

_RD7 = lambda off: 0xFFFE    # a negative (loop-marker) word -> anim_script_forward returns immediately


def test_h7898_state0_creeps_toward_player_when_out_of_range():
    o, d = _o7(x=0x200), _d7(dD=0)                      # |dx|=0x100>>4=0x10 > range 0 -> out of range
    handle_object_7898(o, d, _g7(player_x=0x300), _RD7)
    assert o["xvel"] == 1 and o["state"] == 0           # faces right (obj.x <= player_x), no leap
    o2, d2 = _o7(x=0x400), _d7(dD=0)
    handle_object_7898(o2, d2, _g7(player_x=0x300), _RD7)
    assert o2["xvel"] == (0xFFFF) and o2["state"] == 0  # faces left (obj.x > player_x)


def test_h7898_state0_leaps_when_in_range():
    o, d = _o7(x=0x200), _d7(dD=2, dE=5)               # obj.x <= player_x -> faces right; |dx|=5>>4=0 <= range
    handle_object_7898(o, d, _g7(player_x=0x205), _RD7)
    assert o["state"] == 0xA
    assert o["yvel"] == (5 << 4)                        # def[0xE]<<4
    assert o["xvel"] == (5 << 4)                        # facing right (xvel hi byte 0) -> not mirrored


def test_h7898_leap_mirrors_x_when_facing_left():
    o, d = _o7(x=0x300), _d7(dD=2, dE=5)               # obj.x > player_x -> xvel becomes 0xFFFF, hi&0x50!=0
    handle_object_7898(o, d, _g7(player_x=0x2FF), _RD7)
    assert o["state"] == 0xA and o["xvel"] == (-(5 << 4)) & 0xFFFF


def test_h7898_flying_state_does_nothing():
    o, d = _o7(state=0xA, xvel=0x50, yvel=0x50), _d7()
    handle_object_7898(o, d, _g7(), _RD7)
    assert o["xvel"] == 0x50 and o["yvel"] == 0x50 and o["state"] == 0xA


def test_h7898_dying_state():
    o, d = _o7(state=0xFF, id=0x2000), _d7(d4=1)
    handle_object_7898(o, d, _g7(), _RD7)
    assert o["yvel"] == 0xF                             # dying_state gravity


# -- handle_object_75c4 (idx12 falling/earthquake object, 1030:75C4) --

def _o12(**kw):
    o = dict(x=0x200, id=0x2000 | 0x14F, xvel=0, state=0); o.update(kw); return o

def _d12(**kw):
    d = dict(dD=3, d7=0); d.update(kw); return d


def test_h75c4_state0_sets_velocity_toward_player_and_advances():
    o, d = _o12(x=0x200, state=0), _d12(dD=3)
    handle_object_75c4(o, d, dict(player_x=0x300))      # player right -> +speed
    assert o["xvel"] == (3 << 4) and o["state"] == 1
    o, d = _o12(x=0x300, state=0), _d12(dD=3)
    handle_object_75c4(o, d, dict(player_x=0x200))      # player left -> -speed
    assert o["xvel"] == (-(3 << 4)) & 0xFFFF


def test_h75c4_state1_drawn_resets_timer():
    o, d = _o12(state=1, id=0x2000 | 0x14F), _d12(d7=5)   # drawn (bit13) -> reset
    handle_object_75c4(o, d, dict(player_x=0))
    assert d["d7"] == 0 and o["state"] == 1


def test_h75c4_state1_offscreen_times_out():
    o, d = _o12(state=1, id=0x14F), _d12(d7=0x99)        # not drawn, timer hits 0x9A -> die
    handle_object_75c4(o, d, dict(player_x=0))
    assert d["d7"] == 0x9A and o["state"] == 0xFF


# -- handle_object_78ec (idx6 earthquake / screen-shake driver, 1030:78EC) --

def _o6(**kw):
    o = dict(x=0x200, y=0x100, id=0x2000 | 0x14F, xvel=0, yvel=0, state=0); o.update(kw); return o

def _d6(**kw):
    d = dict(d2=0x1111, d4=1, d6=0, d7=0, dD=0x40, dE=0, dF=0, d10=0, d11=0, d12=0, d13=0, d14=0)
    d.update(kw); return d

def _g6(**kw):
    g = dict(player_x=0x200, player_y=0x100, a30e=0, a310=0, bc0=0, bc1=0, bd0=1,
             ror=0, la=0, lb=0, lc=0, ld=0)
    g.update(kw); return g


def test_h78ec_state0_arms_when_player_in_range():
    o, d = _o6(x=0x200, y=0x100, state=0), _d6(dD=0x40)
    handle_object_78ec(o, d, _g6(player_x=0x210))       # |dx|>>4 = 1 <= 0x40 -> arm
    assert o["state"] == 1 and d["d10"] == 0x18
    # accumulators seeded to objX<<3 / objY<<3 (byte-pair little-endian)
    assert (d["d11"] | (d["d12"] << 8)) == (0x200 << 3) & 0xFFFF
    assert (d["d13"] | (d["d14"] << 8)) == (0x100 << 3) & 0xFFFF


def test_h78ec_state0_out_of_range_stays():
    o, d = _o6(x=0x200, state=0), _d6(dD=0)
    handle_object_78ec(o, d, _g6(player_x=0x400))       # |dx|>>4 = 0x20 > 0 -> no arm
    assert o["state"] == 0


def test_h78ec_state1_close_kick_sets_shake_velocity_and_accumulates():
    # player very close (dist^2 small), bd0 enabled, |dY|<0x30 -> sets [def+0xE]; then accumulate runs
    o, d = _o6(x=0x200, y=0x100, state=1), _d6(dE=0, dF=0, d11=0, d12=0, d13=0, d14=0)
    handle_object_78ec(o, d, _g6(player_x=0x201, player_y=0x100, bd0=1))
    assert d["dE"] != 0                                  # a shake X velocity was kicked
    assert o["x"] == (_s16_ref(d["d11"] | (d["d12"] << 8)) >> 3) & 0xFFFF


def test_h78ec_state1_far_counts_timer_down():
    o, d = _o6(x=0x200, y=0x100, state=1, id=0x2000 | 0x14F), _d6(d10=5)
    handle_object_78ec(o, d, _g6(player_x=0x9000, player_y=0x100, bd0=1))   # far -> a310!=0 -> decrement
    assert d["d10"] == 4


def test_h78ec_dying_state():
    o, d = _o6(state=0xFF, id=0x2000), _d6(d4=1)
    handle_object_78ec(o, d, _g6())
    assert o["yvel"] == 0xF                               # dying_state ran (gravity)


def _s16_ref(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# -- terrain_collision (1030:698C) + slope helper _surface_offset (6A7D) --

from pre2.recovered.object_update import terrain_collision, _surface_offset

def _terrain(mapd=None, propa=None, propb=None, slopes=None):
    mapd = mapd or {}; propa = propa or {}; propb = propb or {}; slopes = slopes or {}
    return dict(read_map=lambda i: mapd.get(i & 0xFFFF, 0),
                prop_a=lambda t: propa.get(t, 0), prop_b=lambda t: propb.get(t, 0),
                slope=lambda t: slopes.get(t, 0), read_word=lambda off: 0xFFFE)


def test_terrain_gravity_when_no_ground():
    o = dict(x=0x105, y=0x205, xvel=0, yvel=0x20, anim_ptr=0x100)
    terrain_collision(o, dict(d4=0), **_terrain())     # empty map -> fall
    assert o["yvel"] == 0x30                            # +0x10 gravity


def test_terrain_gravity_capped_at_0x100():
    o = dict(x=0x105, y=0x205, xvel=0, yvel=0x100, anim_ptr=0x100)
    terrain_collision(o, dict(d4=0), **_terrain())     # yvel already >=0x100 -> no add
    assert o["yvel"] == 0x100


def test_terrain_lands_and_stops_when_non_bouncing():
    # tile_here solid (propB!=0), flat slope, def4&0x20 -> stop on landing
    here = (0x20 << 8) | 0x10
    o = dict(x=0x105, y=0x205, xvel=0, yvel=0x100, anim_ptr=0x100)
    terrain_collision(o, dict(d4=0x20), **_terrain(mapd={here: 7}, propb={7: 1}))
    assert o["yvel"] == 0 and (o["y"] & 0xF) == 0       # snapped to tile, stopped


def test_terrain_bounces_when_fast():
    here = (0x20 << 8) | 0x10
    o = dict(x=0x105, y=0x205, xvel=0, yvel=0x100, anim_ptr=0x100)
    terrain_collision(o, dict(d4=0), **_terrain(mapd={here: 7}, propb={7: 1}))
    assert o["yvel"] == (-0x80) & 0xFFFF                # bounce at -yvel/2 (|0x80|>0x20)


def test_terrain_wall_bounce_reverses_xvel():
    # ahead-above tile is a wall (propA!=0), no climb flag -> neg xvel
    here = (0x20 << 8) | 0x10
    ahead_above = ((0x1F) << 8) | 0x11                  # tx+1 (xvel>0), ty-1
    o = dict(x=0x105, y=0x205, xvel=0x40, yvel=0, anim_ptr=0x100)
    terrain_collision(o, dict(d4=0), **_terrain(mapd={ahead_above: 9}, propa={9: 1}))
    assert o["xvel"] == (-0x40) & 0xFFFF


def test_terrain_starts_climb_when_flag_set():
    ahead_above = ((0x1F) << 8) | 0x11
    o = dict(x=0x105, y=0x205, xvel=0x40, yvel=0, anim_ptr=0x100)
    d = dict(d4=0x40)
    terrain_collision(o, d, **_terrain(mapd={ahead_above: 9}, propa={9: 1}))
    assert (d["d4"] & 0x80) and o["yvel"] == 0xFFF0     # climbing up, 0x80 set


def test_surface_offset_flat_is_signed_raw():
    assert _surface_offset(0x105, 3, lambda t: 0x00) == 0
    assert _surface_offset(0x105, 3, lambda t: 0x05) == 5     # flat (no 0x30 bits) -> raw value
    assert _surface_offset(0x105, 3, lambda t: 0x80) == -128  # flat negative (no 0x30 bits) -> signed byte


def test_surface_offset_slope_rising():
    # slope byte 0x10|0x4 -> rising: (subx//3) + (s&0xf)=4 ; subx = 0x105&0xf = 5 -> 5//3=1 -> 1+4=5
    assert _surface_offset(0x105, 3, lambda t: 0x14) == 5


# -- handle_object_7a60 (idx5 2D-proximity pouncer, 1030:7A60) --

def _o5(**kw):
    o = dict(x=0x200, y=0x100, id=0x14F, xvel=0, yvel=0, state=0, anim_ptr=0x100); o.update(kw); return o

def _d5(**kw):
    d = dict(d2=0x1111, d4=1, d7=0, dD=3, dE=2, dF=5); d.update(kw); return d

def _g5(**kw):
    g = dict(player_x=0x200, player_y=0x100); g.update(kw); return g

_RD5 = lambda off: 0xFFFE    # negative loop-marker word for anim_script_forward


def test_h7a60_state0_creeps_then_leaps_in_range():
    o, d = _o5(x=0x200, y=0x100), _d5(dD=3, dE=2, dF=5)        # player within X(3) and Y(2) tiles -> leap
    handle_object_7a60(o, d, _g5(player_x=0x205, player_y=0x100), _RD5)
    assert o["state"] == 0xA
    assert o["yvel"] == (5 << 4)                                # def[0xF]<<4, UNSIGNED
    assert o["xvel"] == (5 << 4)                                # facing right (obj.x<=player_x) -> not mirrored


def test_h7a60_out_of_x_range_no_leap():
    o, d = _o5(x=0x200), _d5(dD=0, dE=9)                        # |dx|=0x100>>4=0x10 > 0 -> out of X range
    handle_object_7a60(o, d, _g5(player_x=0x300, player_y=0x100), _RD5)
    assert o["state"] == 0 and o["xvel"] == 1                   # faced player, did not leap


def test_h7a60_out_of_y_range_no_leap():
    o, d = _o5(x=0x200, y=0x100), _d5(dD=9, dE=0)              # in X, |dy|=0x100>>4=0x10 > 0 -> out of Y range
    handle_object_7a60(o, d, _g5(player_x=0x205, player_y=0x300), _RD5)
    assert o["state"] == 0


def test_h7a60_leap_mirrors_x_when_facing_left():
    o, d = _o5(x=0x300, y=0x100), _d5(dD=9, dE=9, dF=5)        # obj.x>player_x -> faces left -> xvel mirrored
    handle_object_7a60(o, d, _g5(player_x=0x2FF, player_y=0x100), _RD5)
    assert o["state"] == 0xA and o["xvel"] == (-(5 << 4)) & 0xFFFF


def test_h7a60_state0xA_lands_near_player_y():
    o, d = _o5(state=0xA, y=0x104, yvel=0x50), _d5()           # |y-player_y|=4 <= 8 -> land
    handle_object_7a60(o, d, _g5(player_y=0x100), _RD5)
    assert o["state"] == 0xB and o["yvel"] == 0


def test_h7a60_state0xA_still_flying_when_far_in_y():
    o, d = _o5(state=0xA, y=0x140, yvel=0x50), _d5()           # |y-player_y|=0x40 > 8 -> keep flying
    handle_object_7a60(o, d, _g5(player_y=0x100), _RD5)
    assert o["state"] == 0xA and o["yvel"] == 0x50


def test_h7a60_dying_state():
    o, d = _o5(state=0xFF, id=0x2000), _d5(d4=1)
    handle_object_7a60(o, d, _g5(), _RD5)
    assert o["yvel"] == 0xF                                     # dying_state gravity
