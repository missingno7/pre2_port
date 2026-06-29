"""Byte-exact regression for the 6822 spawner's shared list-init 7585 (see
:mod:`pre2.recovered.object_spawn`).

``init_effect_row`` is a pure function of the caller's spawn count ``cx``; the contract is asserted directly
against the disassembly. The ASM equivalence is proven by a live shadow (0 divergences over 207 + 281 real
spawns across the gorilla + 233821 demos; the audio side-effect via 0x2CC is outside the 0x56A2 list window).
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.object_spawn import (EFFECT_ROW_STRIDE, SCROLL_PHASE, boss_hit_burst, boss_pre_interp,
                                         boss_script_interp, camera_boundary_collision, camera_engine,
                                         camera_offset_lookup, camera_script_interp, camera_state_machine,
                                         camera_target_bounce, hurt_effect, inc_scroll_phase, init_effect_row,
                                         player_cursor_dist, scan_camera_targets, spawn_boss_bolt_1ca,
                                         spawn_boss_bolt_1cb, tick_mode9_boss, tick_mode9_spawn, tick_scroll_cursor)

_LO = 0x56A2


def _golden_case(name, call):
    cases = json.loads((Path(__file__).parent / "fixtures" / "object_spawn" / f"{name}.json").read_text())
    assert cases, f"no fixture cases for {name}"
    for case in cases:
        rw_d = {int(k): v for k, v in case["rw"].items()}
        rb_d = {int(k): v for k, v in case["rb"].items()}
        tile_d = {int(k): v for k, v in case.get("tile", {}).items()}
        golden = {int(k): tuple(v) for k, v in case["writes"].items()}
        writes = call(lambda o: rb_d[o & 0xFFFF], lambda o: rw_d[o & 0xFFFF], lambda o: tile_d[o & 0xFFFF])
        assert writes == golden


def _slot(off):
    return _LO + off * EFFECT_ROW_STRIDE


def test_init_effect_row_empty():
    # cx=0 -> all 8 slots are 0xFFFF terminators, no position written
    w = init_effect_row(0)
    assert all(w[_slot(i) + 4] == (0xFFFF, 2) for i in range(8))
    assert all(_slot(i) not in w for i in range(8))


def test_init_effect_row_partial():
    # cx=3 -> a 3-wide row (X = 0xD, 0x12, 0x17; Y=0xAA; id=0x135) then 5 terminators
    w = init_effect_row(3)
    for i, x in enumerate((0xD, 0x12, 0x17)):
        assert w[_slot(i) + 4] == (0x135, 2)
        assert w[_slot(i)] == (x, 2)
        assert w[_slot(i) + 2] == (0xAA, 2)
    for i in range(3, 8):
        assert w[_slot(i) + 4] == (0xFFFF, 2)


def test_init_effect_row_caps_at_8():
    w = init_effect_row(8)
    assert all(w[_slot(i) + 4] == (0x135, 2) for i in range(8))
    assert init_effect_row(20) == w        # cx>8 capped to 8


def test_inc_scroll_phase_saturates():
    # 757A: [0x6C05] = min([0x6C05]+1, 0xFF). Shadow-verified witnessed + 0-div (gorilla, 7 calls).
    assert inc_scroll_phase(lambda o: 0x00)[SCROLL_PHASE] == (0x01, 1)
    assert inc_scroll_phase(lambda o: 0x40)[SCROLL_PHASE] == (0x41, 1)
    assert inc_scroll_phase(lambda o: 0xFE)[SCROLL_PHASE] == (0xFF, 1)
    assert inc_scroll_phase(lambda o: 0xFF)[SCROLL_PHASE] == (0xFF, 1)   # saturates, no wrap


def test_scan_camera_targets_byte_exact():
    # 80DE: camera-target collision scan + response (composes 8D7B + 757A). Goldens from the live ASM (shadow
    # 0-div over 281 calls / gorilla); cases include 4 collision-response firings (the path the original golden
    # missed, hiding a latent bug). The test replays the recorded reads and asserts the contract.
    _golden_case("scan_camera_targets", lambda rb, rw, tile: scan_camera_targets(rb, rw))


def test_camera_script_interp_byte_exact():
    # 7534 tail: the camera-script bytecode interpreter, composing 93B2 -> 93F6 -> 94DC + the fixed 80DE.
    # Goldens from the live ASM (shadow 0-div over 281 gorilla calls, full response windows; 3 collision cases).
    _golden_case("camera_script_interp", lambda rb, rw, tile: camera_script_interp(rb, rw))


def test_tick_mode9_spawn_head():
    # 6ADD head (last-boss engine entry): seed on first entry ([0xA517]==-1) + spawn [0xA519]>>2 row (7585).
    # Shadow 0-div / 660 calls on the last-boss demo demo_pre2_20260629_141422.
    w = tick_mode9_spawn(lambda o: 0xFF, lambda o: 0xFFFF)               # [0xA517]==-1 -> seed
    assert w[0xA4F7] == (0xA4F9, 2) and w[0xA519] == (0x18, 2) and w[0xA515] == (0, 1) and w[0xA516] == (3, 1)
    assert w[0x56A6] == (0x135, 2)                                       # spawned a 0x18>>2 = 6-wide effect row
    st = {0xA517: 0, 0xA519: 0x10}                                       # already seeded, health 0x10
    w2 = tick_mode9_spawn(lambda o: st.get(o & 0xFFFF, 0) & 0xFF, lambda o: st.get(o & 0xFFFF, 0))
    assert 0xA4F7 not in w2 and w2[0x56A6] == (0x135, 2)                 # no re-seed; spawned 0x10>>2 = 4 slots


def test_spawn_boss_bolts():
    # 6CA7/6CF1: boss-attack projectile spawns into the 0x50A8 pool, composing rng_lcg. Shadow 0-div on the
    # last-boss demo (6CA7 13 calls, 6CF1 18 calls).
    st = {0x50AC: 0xFFFF, 0x2CEC: 1, 0x2CED: 2, 0x2CEE: 3, 0x2CEF: 4}    # slot 0 free, rng state seeded
    rb = lambda o: st.get(o & 0xFFFF, 0) & 0xFF
    rw = lambda o: st.get(o & 0xFFFF, 0)
    w = spawn_boss_bolt_1ca(rb, rw)
    assert w[0x50AC] == (0x1CA, 2) and w[0x50A8] == (0xC8, 2) and w[0x50AA] == (0x58, 2) and 0x2CED in w
    w2 = spawn_boss_bolt_1cb(rb, rw)
    assert w2[0x50AC] == (0x1CB, 2) and w2[0x50AA] == (0, 2) and 0x2CED in w2
    assert spawn_boss_bolt_1ca(lambda o: 0, lambda o: 0x100) == {}       # pool full -> no spawn


def test_boss_script_interp():
    # 6B91: glyph byte (al>=0) at the cursor -> advance [0xA517] + render (seam). Shadow 0-div / 659 calls.
    # (boss_script_interp reads via a byte-level overlay, so the state must be byte-level.)
    def call(state):
        rb = lambda o: state.get(o & 0xFFFF, 0) & 0xFF
        return boss_script_interp(rb, lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8))
    assert call({0xA517: 0x00, 0xA518: 0x05, 0x500: 0x41}) == {0xA517: (0x501, 2)}   # cursor 0x500 -> glyph
    st = {0xA517: 0x00, 0xA518: 0x05, 0x500: 0xFE, 0x501: 0x41, 0xA515: 3,           # 0xFE (spawn 0x1CB) then glyph
          0x50AC: 0xFF, 0x50AD: 0xFF, 0x2CEC: 1, 0x2CED: 2, 0x2CEE: 3, 0x2CEF: 4}
    w2 = call(st)
    assert w2[0x50AC] == (0x1CB, 2) and w2[0xA517] == (0x501, 2)         # spawned 0x1CB, then advanced past 0xFE


def test_boss_pre_interp():
    # 6B1C/6B43: the script-advance + projectile-hit detection. Shadow 0-div / 660 last-boss calls.
    st = {0xA515: 5,                                          # dwell != 0 -> skip the script-advance
          0x4F32: 0x00, 0x4F33: 0x01,                         # slot 0 active ([+4]=0x100)
          0x4F2E: 0xC8, 0x4F2F: 0, 0x4F30: 0x30, 0x4F31: 0,   # X=0xC8, Y=0x30 -> both in the boss zone
          0xA519: 5, 0xA516: 0}                               # boss health 5, cycle 0
    rb = lambda o: st.get(o & 0xFFFF, 0) & 0xFF
    w = boss_pre_interp(rb, lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8))
    assert w[0xA517] == (0xA534, 2) and w[0xA519] == (4, 1) and w[0xA516] == (1, 1)   # hit script + health-- + cycle


def test_tick_mode9_boss_byte_exact():
    # 6ADD..6BDA: the WHOLE composed mode-9 last-boss engine. Verified by a whole-DGROUP shadow on the last-boss
    # demo demo_pre2_20260629_141422 (659 calls, 0 div, excl. audio/DMA + the gated boss-death paths).
    _golden_case("tick_mode9_boss", lambda rb, rw, tile: tick_mode9_boss(rb, rw))


def test_boss_hit_burst():
    # 6BDB: the boss-death diamond burst (4 sprites id 0x2137 via spawn_effect_burst). ASM_MATCHED (the boss
    # survives demo_pre2_20260629_141422, so 6BDB is unwitnessed there); composition asserted vs the disasm.
    rb = lambda o: 0xFF
    rw = lambda o: 0xFFFF if (o & 0xFFFF) in (0x50AC, 0x50BE, 0x50D0, 0x50E2) else 0   # 4 free slots
    w = boss_hit_burst(rb, rw)
    assert w[0x50AC] == (0x2137, 2) and w[0x50A8] == (0xB9, 2) and w[0x50AA] == (0x1E, 2)   # first sprite
    assert w[0x50AE] == (0x20, 2)                                        # first Xvel +0x20


def test_camera_engine_byte_exact():
    # 70D7..7579: the WHOLE composed camera engine (head + dist + cull + state machine + script). Verified by a
    # whole-DGROUP shadow (gorilla 281 calls, 0 div, excl. the moving sound-DMA region + the gated state-6
    # finale) — which caught two latent leaf bugs the per-piece windows missed (80DE response, head gravity).
    _golden_case("camera_engine", lambda rb, rw, tile: camera_engine(rb, rw, tile))


def test_tick_scroll_cursor_byte_exact():
    # 70D7 head: spawn gate + scroll-cursor advance. Goldens from the live ASM at the 0x7172 boundary
    # (shadow 0-div over 413 calls / gorilla + 151845); the test replays the recorded reads + tile lookups.
    _golden_case("tick_scroll_cursor", lambda rb, rw, tile: tick_scroll_cursor(rb, rw, tile))


def test_player_cursor_dist():
    # 7172: direction flag + |X dist| + the state-machine cull. Shadow 0-div / 282 calls (gorilla), cull exact.
    w, cull = player_cursor_dist(lambda o: {0x91FF: 0x100, 0x4F1C: 0x150, 0x9201: 0x100, 0x4F1E: 0x110}[o & 0xFFFF])
    assert w == {0xA3FA: (1, 1), 0xA3FB: (0x50, 2)} and not cull       # cursor left of player, close
    w2, cull2 = player_cursor_dist(lambda o: {0x91FF: 0x200, 0x4F1C: 0, 0x9201: 0, 0x4F1E: 0}[o & 0xFFFF])
    assert w2 == {0xA3FA: (0, 1), 0xA3FB: (0x200, 2)} and cull2        # cursor right of player, X-dist culls


def test_hurt_effect():
    # 824D: damage cooldown tick + hurt burst (composes verified 8D1B). Shadow 0-div over 3 gorilla crush-hits.
    st = {0x6BC9: 3, 0x27D6: 5, 0x4F1C: 0x100, 0x4F1E: 0x80, 0x50AC: 0xFFFF}   # slot 0x50A8 free
    w = hurt_effect(lambda o: st.get(o & 0xFFFF, 0) & 0xFF, lambda o: st.get(o & 0xFFFF, 0))
    assert w[0x6BC9] == (2, 1) and 0x27D6 not in w        # cooldown 3->2, no life lost
    assert w[0xA336] == (0x100, 2) and w[0xA338] == (0x50, 2) and w[0xA33A] == (0x2046, 2)   # fx origin + sprite
    assert w[0x50AC] == (0x2046, 2) and w[0x50A8] == (0x100, 2) and w[0x50AA] == (0x50, 2)   # spawned slot
    assert w[0x50AE] == (0x30, 2) and w[0x50B6] == (0xFF80, 2)         # Xvel (+0x30, cd even) + Yvel
    st2 = {0x6BC9: 0, 0x27D6: 5, 0x4F1C: 0, 0x4F1E: 0x30, 0x50AC: 0xFFFF}
    w2 = hurt_effect(lambda o: st2.get(o & 0xFFFF, 0) & 0xFF, lambda o: st2.get(o & 0xFFFF, 0))
    assert w2[0x6BC9] == (5, 1) and w2[0x27D6] == (4, 1)               # cooldown underflow -> reload 5, lose a life


def test_camera_target_bounce_gates():
    # 81F3 gates (the bounce path itself is shadow-verified: gorilla 62 calls, 0 div, 3 bounces).
    di = 0xA500
    assert camera_target_bounce(lambda o: 0, lambda o: 0xFFFF if (o & 0xFFFF) == di + 4 else 0, di) == {}  # inactive
    assert camera_target_bounce(lambda o: 0, lambda o: 0x100, di) == {}     # active but [di+5]&0x20==0 -> not a boundary


def test_camera_boundary_collision_gated_off():
    # 81B4: [0x6BE4]!=0 disables the whole crush (the active path is shadow-verified: gorilla 31 calls, 0 div).
    assert camera_boundary_collision(lambda o: 1 if (o & 0xFFFF) == 0x6BE4 else 0, lambda o: 0) == {}


def test_camera_state_machine_simple_states():
    # 71AB sequencer: the sub-call-free states (the whole machine is shadow-verified on gorilla: 281 calls,
    # 0 div, states 0/1/2/3/5/6/7; state 4 + the state-6 94F3 finale are ASM_MATCHED / gated).
    def call(state, dist, timer=0):
        d = {0x91FE: state, 0xA3FB: dist, 0xA3F7: timer}
        return camera_state_machine(lambda o: d.get(o & 0xFFFF, 0) & 0xFF, lambda o: d.get(o & 0xFFFF, 0) & 0xFFFF)
    assert call(0, 0x100) == {0x6C05: (0, 1), 0xA401: (0xA427, 2)}        # state 0 far: anchor the camera
    assert call(0, 0x50) == {0xA3F7: (0, 2), 0x91FE: (1, 1)}              # state 0 near: advance to state 1
    assert call(5, 0, timer=5) == {0xA3F7: (6, 2), 0xA401: (0xA47E, 2)}   # state 5 within dwell: hold script
    assert call(5, 0, timer=0x20) == {0xA3F7: (0, 2), 0x91FE: (1, 1)}     # state 5 dwell elapsed: back to state 1


def test_camera_offset_lookup():
    # 94DC: scan the [0xA6ED] stride-6 table for (dx, ax) -> value. Shadow 0-div / 1124 gorilla calls.
    tbl = {0xA6ED: 0x10, 0xA6EF: 0x20, 0xA6F1: 0x1234,    # entry 0: key (0x10, 0x20) -> 0x1234
           0xA6F3: 0x30, 0xA6F5: 0x40, 0xA6F7: 0x5678}    # entry 1: key (0x30, 0x40) -> 0x5678
    rw = lambda o: tbl.get(o & 0xFFFF, 0)
    assert camera_offset_lookup(rw, 0x10, 0x20) == 0x1234       # first entry
    assert camera_offset_lookup(rw, 0x30, 0x40) == 0x5678       # scans past the first entry
