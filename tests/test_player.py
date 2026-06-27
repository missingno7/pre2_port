"""Tests for the recovered player FSM leaves (pre2.recovered.player).

Byte-exact ASM equivalence is proven on live gameplay demos (player_x_integrate 1999/1999 on L1 + 299/299 on
L6; player_y_integrate 2069/2069 + 299/299); these pin the kinematics formulas + the boundary clamps."""
from __future__ import annotations

from pre2.recovered.player import (
    ANIM_ID_TABLE,
    ANIM_SEQ_TABLE,
    TIMER_BYTES,
    TIMER_WORD,
    player_accel,
    player_advance_anim,
    player_friction_dir,
    player_friction_sym,
    player_gravity,
    player_charge_6bce,
    player_dispatch_handler,
    player_emit_trail,
    player_fsm_frontend,
    player_select_anim_id,
    player_set_anim,
    player_state_anim4,
    player_state_anim5,
    player_state_attack,
    player_state_anim8,
    player_state_idle,
    player_state_jump,
    player_state_run,
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


def test_select_anim_id_maps_bitmask_via_table():
    table = {(ANIM_ID_TABLE + i) & 0xFFFF: v for i, v in enumerate([0, 3, 5, 7, 2, 6, 0, 0, 1])}
    rb = lambda off: table.get(off, 0)
    aid, w = player_select_anim_id(1, suppress=0, depth=0, anim_b_state=0, beb=0, read_byte=rb)
    assert aid == 3                      # table[bitmask 1]
    assert w[0x4F1B] == 0                # = depth
    assert w[0x4F2C] == 0 and w[0x6BEB] == 1   # anim changed 0->3 -> reset, then inc
    # depth >= 0x16 overrides anim_id to 8
    assert player_select_anim_id(1, 0, depth=0x16, anim_b_state=0, beb=0, read_byte=rb)[0] == 8
    # suppress -> bitmask forced to 0 -> table[0] == 0
    assert player_select_anim_id(1, suppress=1, depth=0, anim_b_state=0, beb=0, read_byte=rb)[0] == 0
    # no anim change -> no [0x4F2C] reset, beb keeps counting
    _, w2 = player_select_anim_id(1, 0, 0, anim_b_state=3, beb=4, read_byte=rb)
    assert 0x4F2C not in w2 and w2[0x6BEB] == 5


def test_state_run_composes_primitives_byte_exact():
    seq = {0x9000: 0x0177}
    table = {(2 + ANIM_SEQ_TABLE) & 0xFFFF: 0x9000}  # seq_index 2 -> ptr 0x9000
    mem = {0x6BD3: 0x05, 0x4F22: 0x10, 0x4F25: 1, 0x4F24: 0,
           0x6BDB: 1, 0x6BF6: 0x40, 0x4F27: 0x00, 0x4F28: 0x1234}
    rb = lambda off: mem.get(off, 0) & 0xFF
    rw = lambda off: table.get(off, seq.get(off, mem.get(off, 0))) & 0xFFFF
    out = player_state_run(rb, rw)
    assert out[0x6BD3] == 0x06                       # sat_inc frame counter
    assert out[0x4F22] == 0x18                       # accel +0x10 -> 0x20, friction -8 -> 0x18
    assert out[0x4F27] == 1                          # set_anim_b stored anim_id (changed 0->1)
    assert out[0x4F28] == 0x9002                     # advance_anim ptr += 2
    assert out[0x4F20] == 0x0177 and out[0x6BCF] == 0x01   # frame, raw high byte


def test_charge_6bce_grows_capped():
    assert player_charge_6bce(0x00) == 0x02
    assert player_charge_6bce(0x30) == 0x32     # <= 0x30 -> +2
    assert player_charge_6bce(0x31) == 0x31     # > 0x30 -> unchanged


def test_state_anim5_composition_byte_exact():
    seq = {0x9100: 0x0312}
    table = {(0x0A + ANIM_SEQ_TABLE) & 0xFFFF: 0x9100}   # seq_index 0x0A -> ptr 0x9100
    mem = {0x4F27: 0x00, 0x4F28: 0x1111, 0x4F25: 1, 0x4F22: 0x40, 0x4F24: 0, 0x6BCE: 0x10}
    rb = lambda off: mem.get(off, 0) & 0xFF
    rw = lambda off: table.get(off, seq.get(off, mem.get(off, 0))) & 0xFFFF
    out = player_state_anim5(rb, rw)
    assert out[0x6BC8] == 0 and out[0x6BE1] == 4
    assert out[0x4F27] == 5                      # set_anim_b stored anim_id (changed 0->5)
    assert out[0x4F28] == 0x9102 and out[0x4F20] == 0x0312   # advance_anim
    assert out[0x4F22] == 0x40 - 0xC             # friction_sym
    assert out[0x6BCE] == 0x12                   # charge_6bce 0x10 + 2


def test_emit_trail_gated_and_ring_wrap():
    assert player_emit_trail(0x100, 0x200, blink=1, ring_ptr=0x4FBE) is None   # gated (blink & 3)
    w, nptr = player_emit_trail(0x100, 0x200, blink=0, ring_ptr=0x4F76)
    assert w[0x4F76] == 0x100 and w[0x4F78] == 0x200 and w[0x4F7A] == 0x35
    assert nptr == 0x4FBE                          # 0x4F76 - 0x12 < 0x4F76 -> wraps to 0x4FBE
    _, nptr2 = player_emit_trail(0, 0, blink=4, ring_ptr=0x4FBE)
    assert nptr2 == 0x4FBE - 0x12                   # blink 4 -> &3 == 0 (not gated), normal step


def test_state_idle_airborne_applies_friction_only():
    mem = {0x4F22: 0x40, 0x6BF6: 0x40, 0x4F24: 0, 0x6BFE: 0, 0x4F2A: 0x10, 0x6BD1: 2}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = player_state_idle(rb, rw)
    assert out[0x4F22] == 0x2C                      # friction_dir 0x40->0x38, friction_sym ->0x2C
    assert 0x4F20 not in out and 0x4F27 not in out  # airborne: no anim, no [0x4F27] reset


def test_state_jump_falls_to_idle_when_6be0_set():
    mem = {0x6BE0: 1, 0x4F22: 0x40, 0x6BF6: 0x40, 0x4F24: 0, 0x6BFE: 0, 0x4F2A: 0x10, 0x6BD1: 2, 0x4F25: 1}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = player_state_jump(rb, rw)
    assert out[0x4F22] == 0x2C and 0x4F20 not in out   # delegated to idle's airborne path


def test_state_jump_arc_adds_impulse():
    from pre2.recovered.player import ANIM_SEQ_TABLE, JUMP_IMPULSE_TABLE
    seq = {0x9200: 0x0241}
    jt = {(JUMP_IMPULSE_TABLE + 2 * 2) & 0xFFFF: 0xFFEC}        # counter 2 -> impulse -0x14
    table = {(4 + ANIM_SEQ_TABLE) & 0xFFFF: 0x9200}
    mem = {0x6BE0: 0, 0x6BD1: 2, 0x4F2A: 0x00, 0x4F22: 0x00, 0x4F25: 1, 0x4F24: 0,
           0x6BDB: 1, 0x4F27: 0, 0x4F28: 0, 0x6BF6: 0x10, 0x6BC7: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: jt.get(o, table.get(o, seq.get(o, mem.get(o, 0)))) & 0xFFFF
    out = player_state_jump(rb, rw)
    assert out[0x6BD1] == 3                              # counter inc
    assert out[0x4F2A] == 0xFFEC                         # Yvel += -0x14
    assert out[0x4F22] == 0x0C                           # accel +0x10 -> friction_dir x2 (-2,-2)
    assert out[0x4F27] == 2 and out[0x4F28] == 0x9202 and out[0x4F20] == 0x0241


def test_state_anim8_setanim_uses_clobbered_xvel():
    from pre2.recovered.player import ANIM_SEQ_TABLE
    seq = {0x9300: 0x0455}
    table = {(0x10 + ANIM_SEQ_TABLE) & 0xFFFF: 0x9300}
    mem = {0x4F22: 0x40, 0x6BF6: 0x40, 0x4F24: 0, 0x4F27: 0xFF, 0x4F28: 0, 0x4F25: 1}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: table.get(o, seq.get(o, mem.get(o, 0))) & 0xFFFF
    out = player_state_anim8(rb, rw)
    assert out[0x4F22] == 0x2C                       # friction_dir 0x40->0x38, friction_sym ->0x2C
    assert out[0x4F27] == 0x2C                       # set_anim_b al = post-friction Xvel low byte (the gotcha)
    assert out[0x4F28] == 0x9302 and out[0x4F20] == 0x0455


def test_state_anim4_accel_path():
    from pre2.recovered.player import ANIM_SEQ_TABLE
    seq = {0x9400: 0x0688}
    table = {(8 + ANIM_SEQ_TABLE) & 0xFFFF: 0x9400}
    mem = {0x4F22: 0x10, 0x6BF6: 0x10, 0x4F24: 0, 0x6BCE: 0, 0x4F25: 1, 0x6BDB: 1,
           0x4F27: 0xFF, 0x4F28: 0, 0x6BD3: 0x99, 0x6BE1: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: table.get(o, seq.get(o, mem.get(o, 0))) & 0xFFFF
    out = player_state_anim4(rb, rw)
    assert out[0x6BD3] == 0 and out[0x6BE1] == 4 and out[0x6BCE] == 2   # cleared / set / charged
    assert out[0x4F22] == 0x20                       # |Xvel| 0x10 <= 0x20 -> accel +0x10 (clamped 0x20)
    assert out[0x4F27] == 0x10                       # set_anim al = |Xvel| (clobbered)
    assert out[0x4F28] == 0x9402 and out[0x4F20] == 0x0688


def test_fsm_frontend_bitmask_and_facing():
    # right (ec) held alone -> bitmask bit4, turn to face right, [0x6BDB]=ec|ed
    mem = {0x27EC: 0xFF, 0x4F25: 0xFFFF}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    bm, w = player_fsm_frontend(rb, rw)
    assert bm == 0x10                                # ec -> bit4
    assert w[0x4F25] == 1 and w[0x6BEB] == 0         # turned right, run counter reset
    assert w[0x6BDB] == 0xFF
    # e8 held alone -> bitmask bit0; no direction -> no facing change
    mem2 = {0x27E8: 0xFF, 0x4F25: 1}
    rb2 = lambda o: mem2.get(o, 0) & 0xFF
    rw2 = lambda o: mem2.get(o, 0) & 0xFFFF
    bm2, w2 = player_fsm_frontend(rb2, rw2)
    assert bm2 == 0x01 and 0x4F25 not in w2


def test_attack_sound_path_sets_override_flag_and_sfx():
    # attack handler (anim_id 3): a frame whose anim high byte has bit6 set selects the sound path
    # ([0x6BD0] = (~bcf)&0x40 == 0), which emits play_sfx and stores the phase sfx in [0x6BCD].
    SEQ, FT = 0x9000, 0x9100
    mem = {0x7B18: 0, 0x4F27: 0, 0x4F28: 0, 0x4F25: 1, 0x4F24: 0, 0x4F22: 0x40, 0x6BF6: 0x10,
           0x6BD3: 0, 0x6BCE: 0, 0x6BFE: 1, 0x6BD2: 1, 0x7B06: 0x06, 0x7B07: 0x19, 0x7B08: 0}
    words = {(6 + 0x7CDF) & 0xFFFF: SEQ,   # set_anim_b(al=3) seq lookup
             SEQ: 0x4055,                  # anim frame word: high byte 0x40 (bit6 set) -> sound path
             0x7B04: FT, FT: 0x55AA}       # phase frame-table ptr -> immediate terminator
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: words.get(o, mem.get(o, 0) | (mem.get(o + 1, 0) << 8)) & 0xFFFF
    out, sfx = player_state_attack(3, 6, rb, rw)
    assert out[0x6BD0] == 0          # bcf bit6 set -> override flag cleared (sound path)
    assert sfx == [5]                # phase 0 -> play_sfx dl=5
    assert out[0x6BCD] == 0x06       # phase sfx stored
    assert out[0x4F27] == 3          # set_anim_b stored the anim id
    assert out[0x7B19] == 0x19       # phase v19
    assert out[0x4F0E] == 0xFFFF     # [0x6BD2]!=0 -> player render slot marked inactive


def test_dispatch_returns_writes_and_sfx_and_routes_attack():
    mem = {0x4F22: 0x40, 0x6BF6: 0x40, 0x4F24: 0, 0x6BFE: 0, 0x4F2A: 0x10, 0x6BD1: 2, 0x4F25: 1}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    # anim_id 0 routes to the idle handler (here its airborne path); no sound
    w0, s0 = player_dispatch_handler(0, rb, rw)
    assert w0[0x4F22] == 0x2C and s0 == []
    # anim_id 3/6/7 route to the recovered attack handler (no longer fails loud), and emit a play_sfx command
    rb_t = lambda o: 0
    rw_t = lambda o: 0x55AA   # every frame-table walk terminates immediately (0x55AA = the table sentinel)
    w3, s3 = player_dispatch_handler(3, rb_t, rw_t)
    assert isinstance(w3, dict) and s3 == [5]   # phase 0 -> play_sfx dl=5


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
