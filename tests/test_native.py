"""The VM-less native core (pre2/native): the NativeGameState foundation + the main-loop spine roadmap.

The adapter swap itself — the recovered 6822 spawner running over a NativeGameState producing byte-identical
DGROUP to the VM — is verified offline against the demos (native_object_spawn_step: 401 boss + 531 camera
calls, 0 div vs the VM). These tests pin the foundation's accessors, the roadmap shape, and the fail-loud gate.
"""
from __future__ import annotations

import pytest

from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.loop import MAIN_LOOP_SPINE, native_object_spawn_step, spine_coverage
from pre2.native.state import DATA_SEG, NativeGameState

_BASE = DATA_SEG << 4


def _state(**dgroup_bytes):
    data = bytearray(0x100000)
    for off, val in dgroup_bytes.items():
        data[_BASE + off] = val & 0xFF
    return NativeGameState(data)


def test_native_game_state_accessors():
    # NativeGameState IS the 1MB address space; rb/rw read DGROUP (DS-relative), the recovered fns' accessors.
    st = _state(**{0x100: 0x34, 0x101: 0x12})
    assert st.rb(0x100) == 0x34 and st.rw(0x100) == 0x1234
    assert len(st.data) == 0x100000


def test_main_loop_spine_roadmap():
    # The roadmap: every per-frame main-loop call, classified. The VM-less core's coverage = the native share.
    assert len(MAIN_LOOP_SPINE) == 27
    cov = spine_coverage()
    assert cov["native"] == 8 and cov["render"] == 4 and cov["gap"] == 15
    assert all(kind in ("native", "render", "gap") for _, kind, _ in MAIN_LOOP_SPINE)


def test_native_object_spawn_step_noop_when_inactive():
    # Camera off ([0x91FE]==0xFF) and not the boss level ([0x2D8A]!=9): neither branch runs -> no writes, no gap.
    st = _state(**{0x91FE: 0xFF, 0x2D8A: 1})
    before = bytes(st.data)
    native_object_spawn_step(st)
    assert bytes(st.data) == before


def test_native_object_spawn_step_fails_loud_on_boss_death():
    # Mode-9, boss already seeded ([0xA517]!=-1) with health 0 -> the 6C0D death finale is unrecovered -> the
    # VM-less core fail-louds (never a silent ASM fallback).
    st = _state(**{0x91FE: 0xFF, 0x2D8A: 9, 0xA517: 0, 0xA518: 0, 0xA519: 0, 0xA51A: 0})
    with pytest.raises(Pre2HybridGap):
        native_object_spawn_step(st)
