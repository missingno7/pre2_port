"""The VM-less native core (pre2/native): the NativeGameState foundation + the main-loop spine roadmap.

The adapter swap itself — the recovered 6822 spawner running over a NativeGameState producing byte-identical
DGROUP to the VM — is verified offline against the demos (native_object_spawn_step: 401 boss + 531 camera
calls, 0 div vs the VM). These tests pin the foundation's accessors, the roadmap shape, and the fail-loud gate.
"""
from __future__ import annotations

import pytest

from pre2.gaps import Pre2HybridGap
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
    state = native_cold_boot(str(ROOT / "assets"), level=0)
    base = DATA_SEG << 4
    before = bytes(state.data[base + 0x4F0A:base + 0x5732])   # player + object/effect pools
    native_combat_pass(state)
    assert state.data[base + 0xA312] == 0                     # [asm 891C] the full-tolerance flag is restored
    assert bytes(state.data[base + 0x4F0A:base + 0x5732]) == before   # idle -> no combat writes


def test_native_foreground_targets_display_page(monkeypatch):
    # [3721] The foreground tile layer (tiles drawn IN FRONT of sprites) must blit to the DISPLAY page [0x2DD6],
    # not the back page [0x2DD8]. read_foreground_state reads [0x2DD8] (the VM draws 3721 to the page being
    # composed), but native renders the core frame to the display page and never flips the buffers, so the
    # foreground landed on the off-screen buffer (user: "foreground tiles are not in foreground"). native_render
    # retargets it. Guards the page override.
    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import native_cold_boot
    import pre2.native.render as R

    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    st = native_cold_boot(str(ROOT / "assets"), level=0)
    base = DATA_SEG << 4
    disp = st.data[base + 0x2DD6] | (st.data[base + 0x2DD7] << 8)
    fg = R.read_foreground_state(st)
    assert fg.page != disp                                     # raw read targets the BACK page (the bug's premise)
    monkeypatch.setattr(R, "read_foreground_state", lambda s: fg)   # native_render mutates THIS fg in place
    dos = DOSMachine(str(ROOT / "assets")); R.native_load_level_palette(st, dos)
    R.native_render(st, dos, disp, game_root=str(ROOT / "assets"), force_gameplay=True)
    assert fg.page == disp                                     # retargeted to the DISPLAY page


def test_native_light_palette_fade():
    # [asm 6772] The light-pickup DAC fade: native classifies 6772 as 'render' (the gameplay frame skips it), so
    # native_apply_palette_fade reproduces the per-frame ramp on dos.vga_palette. A "lights off" pickup sets
    # [0x6C01]=1/[0x6C03]=0/[0x6C04]=1; the palette must ramp colours 0..15 from the level palette toward the dark
    # 0xACB7 palette and then clear the flags. Guards the direction + completion (was: native kept the static pal).
    from dos_re.dos import DOSMachine, _dac8
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.render import native_apply_palette_fade, native_load_level_palette, _LIGHT_DARK_PAL

    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    st = native_cold_boot(str(ROOT / "assets"), level=0)
    dos = DOSMachine(str(ROOT / "assets"))
    native_load_level_palette(st, dos)
    base = DATA_SEG << 4
    bright = tuple(dos.vga_palette[3])
    dark = (_dac8(st.data[base + _LIGHT_DARK_PAL + 9]), _dac8(st.data[base + _LIGHT_DARK_PAL + 10]),
            _dac8(st.data[base + _LIGHT_DARK_PAL + 11]))            # colour 3 of the 0xACB7 dark palette
    assert bright != dark

    # no active fade + lights on -> the static level palette stands (no-op)
    native_apply_palette_fade(st, dos)
    assert tuple(dos.vga_palette[3]) == bright

    # the light-OFF pickup (id 0xea): fade toward the dark palette
    st.data[base + 0x6C01] = 1; st.data[base + 0x6C02] = 0; st.data[base + 0x6C03] = 0; st.data[base + 0x6C04] = 1
    steps = 0
    while (st.data[base + 0x6C01] | st.data[base + 0x6C02]) and steps < 300:
        native_apply_palette_fade(st, dos); steps += 1
    assert 0 < steps < 300                                          # it converged
    assert tuple(dos.vga_palette[3]) == dark                        # ended at the dark target
    assert st.data[base + 0x6C01] == 0 and st.data[base + 0x6C02] == 0   # [asm 67C8] flags cleared on completion


def test_native_flash_slot_captured_and_re_applied():
    # [asm 2757/28BA] The one-frame OPAQUE flash (hit/death white silhouette): id bit14 = [+5]&0x40 is READ by 26FA
    # to pick the opaque blit, then CLEARED in the same pass — so it is gone by the commit boundary native_render
    # reads. native_object_render_state captures the flashing slots (pre-clear) into state.flash_slots; native_render
    # re-applies bit14 for the draw and RESTORES the record (so the carried-forward state stays == the VM's). Guards
    # both halves — without them a dying/hit enemy renders as its normal colours instead of the white flash.
    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_object_render_state
    from pre2.native.render import native_load_level_palette, native_render

    from pre2.recovered.object_render import (Camera, MODE_NORMAL, MODE_OPAQUE, Sprite, SpriteAttr,
                                              plan_sprite_command)
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    st = native_cold_boot(str(ROOT / "assets"), level=0)
    base = DATA_SEG << 4
    dos = DOSMachine(str(ROOT / "assets")); native_load_level_palette(st, dos)
    disp = st.data[base + 0x2DD6] | (st.data[base + 0x2DD7] << 8)
    slot = 0x4F1C                                                   # set up an active render slot with the flash flag
    st.data[base + slot + 4] = 0x02; st.data[base + slot + 5] = 0x40  # id = 0x4002 (base 0x02, bit14 flash set)
    st.data[base + slot + 0x11] = 5                                 # life

    native_object_render_state(st)                                 # the 26FA record-mutation half
    assert st.flash_slots and slot in st.flash_slots              # captured pre-clear (id bit14 was set)

    # native_render re-applies the flash for the draw, then RESTORES the record + clears the one-shot capture — so
    # the carried-forward state stays == the VM's cleared record (a left-set flag would desync the next frame).
    record_before = bytes(st.data[base + slot:base + slot + 0x12])
    native_render(st, dos, disp, game_root=str(ROOT / "assets"), force_gameplay=True)
    assert st.flash_slots is None                                 # one-shot handoff, cleared
    assert bytes(st.data[base + slot:base + slot + 0x12]) == record_before   # record restored byte-for-byte

    # The flag's EFFECT (pure): with bit14 set the blit is the OPAQUE white silhouette; without it, the normal draw.
    attr = SpriteAttr(width=16, height=16, x_off=0, y_off=0, src_seg=0, src_off=0)
    cam = Camera(cam_x=0, cam_y=0, fine_scroll=0, row_factor=0, dest_page=0, row_stride=0x50, global_shift=1, frame=0)
    flash_cmd = plan_sprite_command(Sprite(x=100, y=100, sprite_id=0x4002, flags=0x40, life=5), attr, cam)
    normal_cmd = plan_sprite_command(Sprite(x=100, y=100, sprite_id=0x0002, flags=0x00, life=5), attr, cam)
    assert int(flash_cmd.mode) == MODE_OPAQUE and int(normal_cmd.mode) == MODE_NORMAL


def test_native_cave_teleport_enters_cave():
    # [asm 52FE/5326] The position-trigger scan raises Pre2CaveTeleport on a table match; driving the
    # native_cave_teleport generator plays the WHOLE transition: the 30C6 fade-out yields, the hidden camera pan
    # (1 unit/step via the real scroll primitives), the destination snap + disarm, the 53D7 mini-pass, the 3054
    # reveal yields, and the frame remainder. Runs over a real cold-booted LEVEL1 state so the pan + mini-pass
    # execute against real level structures.
    from pre2.gaps import Pre2CaveTeleport
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_cave_teleport, native_trigger_scan

    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    st = native_cold_boot(str(ROOT / "assets"), level=0)
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


def test_native_object_spawn_step_handles_boss_death():
    # Mode-9, boss dead ([0xA519]==0): the 6C0D victory-glyph finale is a pure page-blit render seam (no DGROUP
    # gameplay writes), so the VM-less core runs the per-frame head and returns cleanly — NOT a fail-loud gap.
    # (Recovered 2026-07-03; the killing-hit death-burst 6B76 -> boss_hit_burst is verified byte-exact.)
    st = _state({0x91FE: 0xFF, 0x2D8A: 9, 0xA517: 0, 0xA518: 0, 0xA519: 0, 0xA51A: 0})
    native_object_spawn_step(st)   # no raise — the boss-death finale is handled


def test_native_attract_interrupt():
    # 8E98 menu-idle ATTRACT: _native_attract sets demo-playback mode [0x2879]=1, loads the demo's level, and
    # yields gameplay frames; a pending key [0x2874] makes DC1 (0DD6) set [0x6BE5], ending the demo — so it resets
    # [0x2879]=0 and returns (the caller re-shows the menu). Verified in play: idle->attract->key->back to menu.
    from types import GeneratorType
    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.front_end import _native_attract
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    ASSETS = str(ROOT / "assets")
    st = native_cold_boot(ASSETS, level=0)
    st.data[(0x1A0F << 4) + 0x83E] = 0                    # the demo's level (1)
    gen = _native_attract(st, DOSMachine(ASSETS), ASSETS)
    assert isinstance(gen, GeneratorType)
    assert next(gen) is not None                           # the first frame is the title (still live-input mode)
    assert st.data[(0x1A0F << 4) + 0x2879] == 0
    seen_gameplay = False
    n = 0
    for _ in gen:                                          # title -> carte -> gameplay demo ([0x2879]=1)
        n += 1
        if st.data[(0x1A0F << 4) + 0x2879] == 1:           # the gameplay demo is running
            seen_gameplay = True
            st.data[(0x1A0F << 4) + 0x2874] = 0x39         # press a key -> DC1 sets [0x6BE5] -> demo ends
        elif seen_gameplay:
            break                                          # ended -> [0x2879] back to 0 (the menu)
        if n > 4000:
            break
    assert seen_gameplay                                   # the attract reached the gameplay demo
    assert st.data[(0x1A0F << 4) + 0x2879] == 0            # the key press ended it -> back to live input


def test_native_level_warp():
    # 4C74 level-end warp table [0x2cf6]: [0x6be6]==1 is the normal +1 end; >1 warps a main level to its BONUS
    # level [0x2cf6+level] (no +1), and a bonus level (>=0xA) back to its source main level +1. Verified byte-exact
    # vs the ASM 4C69 dispatch; was previously a fail-loud gap (LEVELC/LEVELD... reached via a warp exit).
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.level_state import native_level_end
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    ASSETS = str(ROOT / "assets")

    def next_level(level, mode):
        st = native_cold_boot(ASSETS, level=level)
        st.data[(0x1A0F << 4) + 0x6BE6] = mode
        native_level_end(st, game_root=ASSETS)
        return st.data[(0x1A0F << 4) + 0x2D8A]

    assert next_level(2, 1) == 3          # normal end: +1
    assert next_level(2, 2) == 0x0B       # warp: L3 -> bonus LEVELC
    assert next_level(0x0B, 2) == 3       # warp back: bonus LEVELC (src 2) -> L4


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
    from pre2.gaps import Pre2RespawnTransition
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


def test_native_level_end_preserves_persistent_player_state():
    # [asm 0137 or ax,ax / je 0163] main's 0x141-0155 level-start reset block (lives [0x27D8]=2, BONUS-letters
    # mask [0x6CA7]=0, utensils [0x6CA8]=0, ...) runs ONLY on a FRESH game start (ax!=0). The between-levels flow
    # enters main via 4F65 (`call 5237; xor ax,ax; stc; ret`), so ax=0 and the block is SKIPPED — that persistent
    # state carries into the next level. native_level_end must preserve it (user: collected BONUS letters were
    # resetting each level; lives were too). Guards the persistence across the level advance.
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.level_state import native_level_end
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
    st = native_cold_boot(str(ROOT / "assets"), level=0)
    base = DATA_SEG << 4
    st.data[base + 0x6CA7] = 0x1F                       # all five BONUS letters collected
    st.data[base + 0x27D8] = 5                          # 5 lives
    st.data[base + 0x6CA8] = 0x07                       # some collected utensils
    native_level_end(st, game_root=str(ROOT / "assets"))
    assert st.data[base + 0x2D8A] == 1                  # advanced to level 1
    assert st.data[base + 0x6CA7] == 0x1F               # BONUS letters PERSIST across the level
    assert st.data[base + 0x27D8] == 5                  # lives PERSIST
    assert st.data[base + 0x6CA8] == 0x07               # utensils PERSIST


def test_native_vga_matches_workbench_dos():
    # pre2.native.vga is the standalone's display adapter; the WORKBENCH DOSMachine exposes the identical
    # surface. Pin them together so the two can never drift (constants, DAC expansion, default palette,
    # and the 3C8/3C9 write protocol).
    from dos_re import memory as _m
    from dos_re.dos import DOSMachine, _dac8 as _dac8_vm
    from pre2.native.vga import EGA_APERTURE, EGA_PLANE_STRIDE, NativeVGA, _dac8

    assert EGA_APERTURE == _m.EGA_APERTURE and EGA_PLANE_STRIDE == _m.EGA_PLANE_STRIDE
    assert all(_dac8(v) == _dac8_vm(v) for v in range(64))
    vm = DOSMachine(".")
    nv = NativeVGA()
    assert nv.vga_palette == vm.vga_palette                    # same power-on DAC
    for dos in (vm, nv):                                       # same 3C8/3C9 protocol result
        dos._track_vga_dac_ports(0x3C8, 5, 8)
        for v in (0x3F, 0x20, 0x01, 0x00, 0x10, 0x3F):
            dos._track_vga_dac_ports(0x3C9, v, 8)
    assert nv.vga_palette[5:7] == vm.vga_palette[5:7]


def test_level_end_tally_vs_curtain_dispatch():
    # [asm 4C69] tally ONLY for: normal end on a main level, or a warp OUT of a bonus level (reverse lookup ->
    # the 4CBA normal path). Warp INTO a bonus level (4C8F) + a bonus level's own end (4CC1) = the 30C6 curtain.
    from pre2.native.level_state import level_end_takes_tally
    assert level_end_takes_tally(mode=1, level=3)            # normal end, main level -> TALLY (4CCB)
    assert not level_end_takes_tally(mode=2, level=3)        # warp INTO the bonus level -> curtain (4C8F)
    assert not level_end_takes_tally(mode=1, level=0xC)      # bonus level's own end -> curtain (4CC1)
    assert level_end_takes_tally(mode=2, level=0xC)          # warp OUT of the bonus level -> TALLY (4C7E->4CBA)


def test_native_gameover_scene_state_golden():
    # [asm 9B23/9CC0] The GAME OVER scene state driver (setup + tick), proven byte-exact vs the ASM by a
    # 60-frame lockstep on snapshot 151024 (0 mismatches, incl. the letters' bounce, the bird orbits + sort +
    # near-side handoff, and the 39DF RNG state). This golden pins the transcription over the deterministic
    # boot-constants state (120 ticks of the slot region + the scroll byte).
    import hashlib

    from pre2.native.boot_data import build_boot_memory
    from pre2.native.gameover_scene import native_gameover_setup, native_gameover_tick

    base = DATA_SEG << 4
    st = NativeGameState(build_boot_memory())
    native_gameover_setup(st)
    assert st.data[base + 0x4F20] != 0xFF                        # the letters spawned (slot 0x4F1C occupied)
    h = hashlib.sha1()
    for f in range(120):
        st.data[base + 0x6BD5] = f & 0xFF
        st.data[base + 0x6BD6] = 0
        native_gameover_tick(st)
        h.update(st.data[base + 0x4F0A:base + 0x5732])
        h.update(bytes([st.data[base + 0x6BC4]]))
    assert st.data[base + 0x6BC4] == 120                         # the scroll advanced 1px/tick (cap 0xB9 @185)
    assert h.hexdigest() == "2f2c3f77e6e043c4a90f44d583d671b672c9a178"


def test_object_anim_scale7_zoom_shake():
    # [asm 68AA-68B1] zoom level 7 ([0x6BE2]==7): identical to any other non-zero scale (the 0xA801 region
    # remap, ASM-verified on snapshot 143131) PLUS the screen-shake arm — AnimResult.shake=True, which the
    # object walker turns into [0x6BEA]=9 (consumed by the recovered apply_camera_shake). Was a fail-loud
    # ObjectScaleUnsupported ("zoom+shake not witnessed") that a standalone player hit in the wild.
    from pre2.recovered.object_update import advance_animation

    words = {0x100: 0x0010,                                   # the script frame at ptr 0x100
             0xA801: 0x0100, 0xA803: 0x0200, 0xA805: 0x0042}  # one remap entry [0x100..0x200] -> 0x42
    rd = lambda o: words.get(o, 0)                            # noqa: E731
    r6 = advance_animation(0x100, rd, 0x0000, 0, 6)           # scale 6: remap, NO shake
    r7 = advance_animation(0x100, rd, 0x0000, 0, 7)           # scale 7: SAME remap + shake
    assert r6.shake is False and r7.shake is True
    assert r7.sprite_id == r6.sprite_id and r7.script_ptr == r6.script_ptr   # identical remap outcome
    assert r7.sprite_id == 0x42 + 0x35                        # frame 0x148 in [lo,hi] -> remapped + 0x35
    assert r7.script_ptr == 0x100                             # the remap FREEZES the script (no advance)
    r0 = advance_animation(0x100, rd, 0x0000, 0, 0)           # scale 0: plain advance, no shake
    assert r0.shake is False and r0.script_ptr == 0x102
