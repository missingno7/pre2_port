"""The VM-less native core (pre2/native): the NativeGameState foundation + the main-loop spine roadmap.

The adapter swap itself — the recovered 6822 spawner running over a NativeGameState producing byte-identical
DGROUP to the VM — is verified offline against the demos (native_object_spawn_step: 401 boss + 531 camera
calls, 0 div vs the VM). These tests pin the foundation's accessors, the roadmap shape, and the fail-loud gate.
"""
from __future__ import annotations

import pytest

from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.loop import MAIN_LOOP_SPINE, native_object_spawn_step, spine_coverage
from pre2.native.player import native_player_step
from pre2.native.state import DATA_SEG, NativeGameState

_BASE = DATA_SEG << 4


def _state(dgroup_bytes):
    data = bytearray(0x100000)
    for off, val in dgroup_bytes.items():
        data[_BASE + off] = val & 0xFF
    return NativeGameState(data)


def test_native_game_state_accessors():
    # NativeGameState IS the 1MB address space; rb/rw read DGROUP (DS-relative), the recovered fns' accessors.
    st = _state({0x100: 0x34, 0x101: 0x12})
    assert st.rb(0x100) == 0x34 and st.rw(0x100) == 0x1234
    assert len(st.data) == 0x100000


def test_main_loop_spine_roadmap():
    # The roadmap: every per-frame main-loop call, classified. The VM-less core's coverage = the native share.
    assert len(MAIN_LOOP_SPINE) == 27
    cov = spine_coverage()
    # the whole loop is collapsed: every call is a recovered gameplay system or a render call — no raw gaps.
    # (event-driven paths run as idle-no-op / armed-fail-loud, the recovered "native" pattern, so kind == native.)
    assert cov["native"] == 17 and cov["render"] == 10 and cov["gap"] == 0
    assert all(kind in ("native", "render", "gap") for _, kind, _ in MAIN_LOOP_SPINE)


def test_native_object_spawn_step_noop_when_inactive():
    # Camera off ([0x91FE]==0xFF) and not the boss level ([0x2D8A]!=9): neither branch runs -> no writes, no gap.
    st = _state({0x91FE: 0xFF, 0x2D8A: 1})
    before = bytes(st.data)
    native_object_spawn_step(st)
    assert bytes(st.data) == before


def test_native_object_spawn_step_fails_loud_on_boss_death():
    # Mode-9, boss already seeded ([0xA517]!=-1) with health 0 -> the 6C0D death finale is unrecovered -> the
    # VM-less core fail-louds (never a silent ASM fallback).
    st = _state({0x91FE: 0xFF, 0x2D8A: 9, 0xA517: 0, 0xA518: 0, 0xA519: 0, 0xA51A: 0})
    with pytest.raises(Pre2HybridGap):
        native_object_spawn_step(st)


def test_native_player_step_fails_loud_on_pause():
    # The pause spin ([0x2830]!=0) isn't a gameplay-state path -> the native player step fail-louds rather than
    # silently skipping the update. (Dormant in normal play.)
    st = _state({0x2830: 1})
    with pytest.raises(Pre2HybridGap):
        native_player_step(st)


def test_native_player_step_fails_loud_on_death():
    # The death/respawn branch (65AF, when [0x6BE4]==0 and the death flag [0x282F]!=0) is unrecovered for the
    # native step -> fail loud, never a silent ASM fallback.
    st = _state({0x6BE4: 0, 0x282F: 1})
    with pytest.raises(Pre2HybridGap):
        native_player_step(st)


def test_native_level_state_raises_respawn_transition():
    # The respawn (4C69's [0x6be4]==1 -> 4F6C) is a MULTI-FRAME transition (the 60-frame death-bounce), so the
    # per-frame dispatcher SIGNALS it (Pre2RespawnTransition) rather than running it blocking in-loop — running it
    # blocking made the runner render only the end state (instant respawn, no animation). native_frame_step drives
    # it, rendering each frame. (Regression guard for "you respawn immediately, before the death animation plays".)
    from pre2.checkpoints.common import Pre2RespawnTransition
    from pre2.native.loop import native_level_state
    st = _state({0x6BE4: 1, 0x6BE5: 0, 0x6BE6: 0})
    with pytest.raises(Pre2RespawnTransition):
        native_level_state(st)


def test_respawn_handlers_are_per_frame_generators():
    # native_4f6c (respawn) + native_death_bounce_509d (the 60-frame bounce) MUST stay generators that yield once
    # per frame, so the runtime renders each frame of the animation. The deep per-frame byte-exactness vs the ASM
    # 509d loop is proven by pre2/probes/probe_native_respawn_anim.py; this just pins the per-frame SHAPE.
    import inspect

    from pre2.native.level_state import native_4f6c
    from pre2.native.loop import native_death_bounce_509d
    assert inspect.isgeneratorfunction(native_4f6c)
    assert inspect.isgeneratorfunction(native_death_bounce_509d)


def test_native_camera_follow_gated_off():
    # [0x6BD9]!=0 gates the whole camera follow off (564E); it only clears cs:[0x6771] (already 0) -> no change.
    from pre2.native.camera_scroll import native_camera_follow
    st = _state({0x6BD9: 1})
    before = bytes(st.data)
    native_camera_follow(st)
    assert bytes(st.data) == before


def test_native_v_scroll_down_accumulates():
    # The vertical-down primitive (33AD) adds dl to the sub-tile accumulator [0x6BC4]; the camera cell [0x2DE6]
    # only advances when it crosses 0x10. Here 0x08+5 < 0x10 -> no cell advance.
    from pre2.native.camera_scroll import _v_scroll_down
    st = _state({0x2CF5: 0xFF, 0x2DE6: 0x10, 0x6BC4: 0x08})
    assert _v_scroll_down(st, 5) is True
    assert st.rb(0x6BC4) == 0x0D and st.rw(0x2DE6) == 0x10


def test_native_v_scroll_down_at_limit():
    # At the bottom camera limit ([0x2DE6] >= [0x2CF5]-0xB) the down primitive cannot scroll -> returns False.
    from pre2.native.camera_scroll import _v_scroll_down
    st = _state({0x2CF5: 0x20, 0x2DE6: 0x15})   # limit = 0x20-0xB = 0x15; 0x15 >= 0x15
    assert _v_scroll_down(st, 5) is False


def test_native_input_drives_key_table():
    # apply_input writes the raw per-scancode key table DC1 reads (0xFF down / 0 up) at [0x27F4 + scancode] —
    # the host-input seam, no keyboard ISR.
    from pre2.native.input import apply_input, KEY_TABLE, SCAN_RIGHT, SCAN_LEFT, SCAN_FIRE
    st = _state({})
    apply_input(st, right=True, fire=True)
    assert st.rb(KEY_TABLE + SCAN_RIGHT) == 0xFF and st.rb(KEY_TABLE + SCAN_FIRE) == 0xFF
    assert st.rb(KEY_TABLE + SCAN_LEFT) == 0
    apply_input(st, right=False, fire=False)          # release
    assert st.rb(KEY_TABLE + SCAN_RIGHT) == 0 and st.rb(KEY_TABLE + SCAN_FIRE) == 0
