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


def test_native_idle_timer_tick_reproduces_vm():
    # [0x27F0] is a free-running 32-bit wall-clock counter the timer ISR bumps every 4th tick (when the mod-4 phase
    # cs:[0x1d6b] wraps); the idle-fidget animation reads it. The main loop's 3-retrace wait fires exactly 3 timer
    # ticks/frame, so native advances it deterministically. These checkpoints ARE the VM's captured per-tick
    # [0x27F0]/phase sequence (the menu->L1 demo, seed [0x27F0]=354 / phase 1) — where the old frozen counter made
    # the idle fidget diverge at tick 212.
    from pre2.native.loop import native_idle_timer_tick
    CS = 0x1030 << 4
    st = _state({0x27F0: 354 & 0xFF, 0x27F1: 354 >> 8, 0x27F2: 0, 0x27F3: 0})
    st.data[CS + 0x1D6B] = 1                                # the timer phase captured at the gameplay seed
    want = {1: (355, 0), 2: (355, 3), 3: (356, 2), 4: (357, 1), 5: (358, 0)}
    for f in range(1, 6):
        native_idle_timer_tick(st)
        assert (st.rw(0x27F0), st.data[CS + 0x1D6B]) == want[f], f"frame {f}"
    for _ in range(6, 213):
        native_idle_timer_tick(st)
    assert st.rw(0x27F0) == 513                             # the VM's [0x27F0] at tick 212 (byte-exact)


def test_main_loop_spine_roadmap():
    # The roadmap: every per-frame main-loop call, classified. The VM-less core's coverage = the native share.
    assert len(MAIN_LOOP_SPINE) == 27
    cov = spine_coverage()
    # the whole loop is collapsed: every call is a recovered gameplay system or a render call — no raw gaps.
    # (event-driven paths run as idle-no-op / armed-fail-loud, the recovered "native" pattern, so kind == native.)
    assert cov["native"] == 18 and cov["render"] == 9 and cov["gap"] == 0   # 88D7 combat pass is now native
    assert all(kind in ("native", "render", "gap") for _, kind, _ in MAIN_LOOP_SPINE)


def test_native_combat_pass_idle_no_op():
    # [asm 88D7] The combat/pickup pass is a byte-exact no-op on an idle frame (no thrown weapons, no enemy in
    # range): it must set [0xA312]=1 across the scan and restore it to 0, and touch no gameplay state. This
    # guards the wiring + the flag restore; the hit/kill/collect paths compose already-shadow-verified leaves.
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_combat_pass

    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    state = native_cold_boot(str(ROOT / "assets"), str(ROOT / "artifacts" / "pre2_boot_image.zz"), level=0)
    base = DATA_SEG << 4
    before = bytes(state.data[base + 0x4F0A:base + 0x5732])   # player + object/effect pools
    native_combat_pass(state)
    assert state.data[base + 0xA312] == 0                     # [asm 891C] the full-tolerance flag is restored
    assert bytes(state.data[base + 0x4F0A:base + 0x5732]) == before   # idle -> no combat writes


def test_native_cave_teleport_enters_cave():
    # [asm 52FE/5326] The position-trigger scan raises Pre2CaveTeleport on a table match; driving the
    # native_cave_teleport generator plays the WHOLE transition: the 30C6 fade-out yields, the hidden camera pan
    # (1 unit/step via the real scroll primitives), the destination snap + disarm, the 53D7 mini-pass, the 3054
    # reveal yields, and the frame remainder. Runs over a real cold-booted LEVEL1 state so the pan + mini-pass
    # execute against real level structures.
    from pre2.checkpoints.common import Pre2CaveTeleport
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_cave_teleport, native_trigger_scan

    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    st = native_cold_boot(str(ROOT / "assets"), str(ROOT / "artifacts" / "pre2_boot_image.zz"), level=0)
    base = DATA_SEG << 4
    ww = lambda o, v: st.data.__setitem__(slice(base + o, base + o + 2), bytes((v & 0xFF, (v >> 8) & 0xFF)))  # noqa: E731
    ww(0x4F1C, 0x7E << 4); ww(0x4F1E, 0x2E << 4)              # player pixel pos -> tile (col 0x7E, row 0x2E) = 0x2E7E
    st.data[base + 0x6BE1] = 1                                # armed
    st.data[base + 0x6BC5] = 0                                # momentum dormant
    ww(0x8367, 0x2E7E); ww(0x8369, 0x0032); ww(0x836B, 0x0636); st.data[base + 0x836D] = 0   # table entry 0

    try:
        native_trigger_scan(st)
        raise AssertionError("expected Pre2CaveTeleport")
    except Pre2CaveTeleport as tp:
        phases = [p[0] for p in native_cave_teleport(st, tp.si)]
    assert phases.count("fade") == 9                          # the 30C6 fade-out curtain steps
    assert phases.count("reveal") == 10                       # the 3054 center-out reveal strip-pairs
    assert phases.count("pan") > 0                            # the hidden camera pan stepped
    assert (st.rw(0x4F1C), st.rw(0x4F1E)) == (0x0360, 0x0060)              # player at the cave destination tile<<4
    assert (st.data[base + 0x2DE4], st.data[base + 0x2DE6]) == (0x32, 0x00)   # camera panned to the destination
    assert st.data[base + 0x6BE1] == 0                                    # trigger disarmed


def test_native_proximity_trigger_mapmod():
    # [asm 53F6/5427] The proximity-trigger map modification (breakable wall / opening passage): a FIRED entry
    # ([+4]==0xFFFE) shifts its height x width tile block UP one row in the level map ([0x2DDA]) and reveals a
    # fresh bottom row from the level asset ([0x2875]:[si+6]), counting down [si+8]. Craft a 1-wide x 2-high block.
    from pre2.native.loop import native_proximity_trigger
    from pre2.native.state import NativeGameState

    st = NativeGameState(bytearray(0x100000))
    base = DATA_SEG << 4
    ww = lambda o, v: st.data.__setitem__(slice(base + o, base + o + 2), bytes((v & 0xFF, (v >> 8) & 0xFF)))  # noqa: E731
    ww(0x2DDA, 0x3000); ww(0x2875, 0x4000)                   # level-map seg 0x3000, asset seg 0x4000
    st.data[base + 0x6BD5] = 0                               # [0x6BD5]&3==0 -> an active map-mod frame
    ww(0x4F1C, 0x800); ww(0x4F1E, 0x800)                     # player tile 0x8080 (far from the empty entries' tile 0)
    ww(0x83F3, 0x200); st.data[base + 0x83F5] = 1; st.data[base + 0x83F6] = 2   # entry0: anchor 0x200, w1 h2
    ww(0x83F3 + 4, 0xFFFE); ww(0x83F3 + 6, 0x50); st.data[base + 0x83F3 + 8] = 2  # fired, srcptr 0x50, cnt 2
    st.data[0x30000 + 0x200] = 0xBB                          # map tiles (below the anchor) + the reveal source
    st.data[0x30000 + 0x300] = 0xCC
    st.data[0x40000 + 0x50] = 0xDD

    native_proximity_trigger(st)
    assert st.data[0x30000 + 0x100] == 0xBB                  # shifted up one row (from 0x200)
    assert st.data[0x30000 + 0x200] == 0xCC                  # shifted up one row (from 0x300)
    assert st.data[0x30000 + 0x300] == 0xDD                  # fresh bottom row revealed from the asset
    assert st.rw(0x83F3) == 0x100                            # [si] -= 0x100
    assert st.rw(0x83F3 + 6) == 0x4F                         # [si+6] -= width
    assert st.data[base + 0x83F3 + 8] == 1                   # [si+8] countdown
    assert st.data[base + 0x83F3 + 4] == 0xFE               # still fired (cnt not yet 0)
    assert st.data[base + 0x6BEA] == 7                       # camera shake armed


def test_native_sync_render_state_advances_animation():
    # native_sync_render_state must advance the animated-tile remap cycle (1030:367D) that the gameplay pass omits,
    # so the standalone renders animated tiles (waving foliage, water, ...) at the SAME frame the VM displays. Proven
    # vs the pure-ASM oracle: without it the whole-scene render diverges on every animation-advance frame (~1 in 4);
    # with it the gorilla level-3 gameplay is pixel-exact (150/150 frames). Here the mechanics: on a throttle-wrap
    # frame the remap pointer [0x6BC2] steps +0x100; otherwise only the throttle [0x6BD4] increments.
    from pre2.native.render import native_sync_render_state
    # [0x6BBD]=1 animated tiles present; [0x6BF6]=0 slow -> mask 3; throttle 3 -> (3+1)&3==0 -> ADVANCE.
    st = _state({0x6BBD: 1, 0x6BC2: 0x88, 0x6BC3: 0x66, 0x6BD4: 0x03, 0x6BF6: 0})
    native_sync_render_state(st)
    assert st.rw(0x6BC2) == 0x6788 and st.rb(0x6BD4) == 0x04          # remap ptr stepped 6688 -> 6788
    # a non-wrap throttle increments the counter but does NOT step the pointer.
    st2 = _state({0x6BBD: 1, 0x6BC2: 0x88, 0x6BC3: 0x66, 0x6BD4: 0x00, 0x6BF6: 0})
    native_sync_render_state(st2)
    assert st2.rw(0x6BC2) == 0x6688 and st2.rb(0x6BD4) == 0x01        # (0+1)&3 != 0 -> no step
    # gate: no animated tiles ([0x6BBD]=0) -> nothing advances at all.
    st3 = _state({0x6BBD: 0, 0x6BC2: 0x88, 0x6BC3: 0x66, 0x6BD4: 0x03})
    native_sync_render_state(st3)
    assert st3.rw(0x6BC2) == 0x6688 and st3.rb(0x6BD4) == 0x03


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


def test_native_load_level_palette():
    # 0ba0 port: [0x2d8a]=level selects the palette pointer [0x2d00+level*2]; its 16 RGB triples (6-bit DAC)
    # load into dos.vga_palette[0..15] (and ONLY those), so a standalone --level N gets its own colours.
    from types import SimpleNamespace

    from dos_re.dos import _dac8
    from pre2.native.render import native_load_level_palette
    st = _state({0x2D8A: 3, 0x2D00 + 3 * 2: 0x00, 0x2D00 + 3 * 2 + 1: 0x30})   # level 3 -> palette ptr 0x3000
    for i in range(48):
        st.data[_BASE + 0x3000 + i] = i
    dos = SimpleNamespace(vga_palette=[(0, 0, 0)] * 256)
    native_load_level_palette(st, dos)
    assert dos.vga_palette[0] == (_dac8(0), _dac8(1), _dac8(2))
    assert dos.vga_palette[15] == (_dac8(45), _dac8(46), _dac8(47))
    assert dos.vga_palette[16] == (0, 0, 0)                        # only colours 0..15 are touched (cx=0x10)


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
