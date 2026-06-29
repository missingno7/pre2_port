"""Byte-exact regression for the 6822 spawner's shared list-init 7585 (see
:mod:`pre2.recovered.object_spawn`).

``init_effect_row`` is a pure function of the caller's spawn count ``cx``; the contract is asserted directly
against the disassembly. The ASM equivalence is proven by a live shadow (0 divergences over 207 + 281 real
spawns across the gorilla + 233821 demos; the audio side-effect via 0x2CC is outside the 0x56A2 list window).
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.object_spawn import (EFFECT_ROW_STRIDE, SCROLL_PHASE, inc_scroll_phase,
                                         init_effect_row, player_cursor_dist, scan_camera_targets,
                                         tick_scroll_cursor)

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
    # 80DE: camera-target collision scan (composes verified 8D7B). Goldens from the live ASM (shadow 0-div over
    # 281 calls / gorilla); the test replays the recorded reads and asserts the contract.
    _golden_case("scan_camera_targets", lambda rb, rw, tile: scan_camera_targets(rb, rw))


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
