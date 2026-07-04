"""The per-frame main-loop spine and the VM-less gameplay step.

``MAIN_LOOP_SPINE`` is the roadmap: every per-frame ``call`` the original main loop (1030:0214..026D) issues,
tagged ``native`` (a recovered gameplay system, wired below as islands land), ``render`` (a draw routine — the
faithful renderer's job, not the gameplay step), or ``gap`` (not yet recovered — the VM-less core fail-louds
here, which *is* the next concrete task). Coverage grows as gaps are recovered; nothing is ever silently
skipped.

The wired steps run the *same* recovered functions the hybrid hooks call, over a :class:`NativeGameState`
instead of VM memory — the migration's adapter swap.
"""
from __future__ import annotations

from collections import Counter

from pre2.bridge.object_spawn import apply_ds, readers, tile_reader
from pre2.bridge.object_tick import LiveWalkerMem
from pre2.gaps import (Pre2CaveTeleport, Pre2GameComplete, Pre2GameOverTransition, Pre2HybridGap,
                                     Pre2LevelEndTransition, Pre2RespawnTransition)
from pre2.recovered.effects_update import (tick_debris_pool, tick_particles, tick_popup_ring,
                                           tick_projectiles)
from pre2.bridge import object_render as _obj_render
from pre2.recovered.object_inject import second_pass_tick
from pre2.recovered.object_particles import project_particles
from pre2.recovered.object_render import plan_record_update, plan_sprite
from pre2.recovered.object_spawn import Pre2SpawnGap, camera_engine, tick_level6_boss, tick_mode9_boss
from pre2.recovered.object_tick import object_tick
from pre2.recovered.terrain_entities import tick_terrain_entities
from pre2.native.state import DATA_SEG
from pre2.native.camera_scroll import _v_scroll_down, _v_scroll_up, native_camera_follow
from pre2.bridge.camera_pan import apply_camera_pan
from pre2.native.player import native_player_interaction, native_player_step


def _apply_bytes(state, writes) -> None:
    """Apply a byte-level ``{offset: value}`` DGROUP contract (e.g. tick_terrain_entities' overlay writes)."""
    base = DATA_SEG << 4
    for off, val in writes.items():
        state.data[(base + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF


class _NativeCpuView:
    """A minimal ``cpu``-like view of a NativeGameState for bridges that take a ``cpu`` (they only read
    ``cpu.mem.data`` + the fixed segments). No registers, no execution — just the memory image and DS=0x1A0F /
    CS=0x1030, so e.g. object_tick's ``LiveWalkerMem`` runs over native state unchanged."""

    __slots__ = ("mem", "s")

    def __init__(self, state):
        self.mem = state                                  # state.data is the 1 MB address space
        self.s = type("Segs", (), {"ds": 0x1A0F, "cs": 0x1030})()

# (addr, kind, note) for every per-frame main-loop call, in order. kind in {"native","render","gap"}.
MAIN_LOOP_SPINE = [
    (0x581E, "native", "tick_popup_ring (effects-update popup)"),
    (0x88D7, "native", "native_combat_pass: the combat/pickup pass (4 projectiles + player vs enemies 8C21 then "
                       "bonus tiles 899E) — damage/kill, effect+debris burst pools, secret-tile collects, [0x4f2a] bounce"),
    (0x6822, "native", "object system: camera_engine/tick_level6_boss(6D34)/tick_mode9_boss -> object_tick(684E) -> 2nd pass(6913)"),
    (0x6210, "native", "tick_projectiles (effects-update)"),
    (0x60FE, "native", "tick_particles (effects-update)"),
    (0x60DF, "native", "tick_debris_pool (effects-update)"),
    (0x4907, "native", "tick_terrain_entities"),
    (0x5850, "native", "native_player_step: the WHOLE player update — DC1 input + FSM + X/Y integrate + "
                       "collision(5A96) + timers, over NativeGameState (DGROUP byte-exact 144/144). 454E render "
                       "([0x6CA2-6CA6]) + sprite-pos ([0x4F0A-0D] when suppressed) + 6294/[0x2830] waits excluded; "
                       "death/pause/cheat/active-momentum fail loud (dormant in play)"),
    (0x8295, "native", "native_player_interaction: player<->world pass (loop1 stomp/hurt/die + loop2 ~25 "
                       "pickups), wired into the native frame"),
    (0x8922, "native", "project_particles: effect source-list [0x8F1D] animation + render-slot [0x52E8] projection"),
    (0x52FE, "native", "native_trigger_scan: position-trigger (player tile coords vs [0x8367] table) -> "
                       "teleport 5326; byte-exact no-op when unarmed ([0x6BE1]==0, always in demos); armed "
                       "scan/teleport fails loud (unwitnessed)"),
    (0x53F6, "native", "native_proximity_trigger: 15-entry proximity-trigger scan [0x83F3] -> map mod (653D) "
                       "when the player nears/fired a trigger; byte-exact no-op when none fire (always in demos); "
                       "the firing map-mod path fails loud (unwitnessed)"),
    (0x5643, "native", "native_camera_follow: per-frame H (57A8->apply_camera_pan) + V (5663->33AD/3363) camera "
                       "follow/scroll; reproduces the camera-scroll state (DGROUP byte-exact 173/173), the plane "
                       "redraw is VRAM (renderer's job)"),
    (0x3668, "render", "frame redraw cluster -> faithful renderer"),
    (0x35A1, "render", "dirty-grid redraw -> faithful renderer"),
    (0x3A27, "render", "scroll-copy window -> faithful renderer"),
    (0x4B8E, "render", "particles_draw: pixels -> faithful renderer; STATE half (advance-Y writeback + slot kill "
     "on the [0x7DE6] point-particle array) extracted into native_gameplay_frame (native_particle_consume)"),
    (0x26FA, "render", "moving-sprite renderer: pixels -> faithful renderer; STATE half (life/flags record "
     "mutation + [0x6bd5] tick) extracted into native_gameplay_frame (native_object_render_state)"),
    (0x3721, "render", "foreground-tile pass -> faithful renderer"),
    (0x54AB, "native", "native_firefly_step (swarm sim + RNG)"),
    (0x3922, "native", "native_scroll_script (counter + [0x6bf6] wind accumulate + LEVELG snow: flake array/rng "
                       "state + white-pixel plot list for the renderer)"),
    (0x4C69, "native", "native_level_state (idle no-op; death/respawn/level-end armed fails loud)"),
    (0x45AF, "render", "45AF respawn-animation draw -> faithful renderer"),
    (0x44FB, "render", "4509+1C65 render/timing helper"),
    (0x6772, "render", "render-frame commit (-> faithful renderer)"),
    (0x67D7, "native", "native_special_event: BONUS-letters reward spawn ([0x6ca7]==0x1f -> 8D1B) + the [0x6ca8] "
     "0x38-group completion; idle no-op"),
    (0x4C30, "native", "native_camera_shake"),
]


def spine_coverage() -> Counter:
    """The VM-less roadmap as counts: how many main-loop calls are native / render / still gaps."""
    return Counter(kind for _, kind, _ in MAIN_LOOP_SPINE)


def native_object_spawn_step(state) -> None:
    """Run the recovered 6822 spawner branches over the :class:`NativeGameState` — the first native-driven
    main-loop call, no VM. Mirrors the 6822 dispatch: ``camera_engine`` when the camera is active
    ([0x91FE]!=0xFF), ``tick_level6_boss`` (6D34) in the level-6 inside-a-tree level ([0x2D8A]==5, gated on
    [0x8166]&0xFE==0 & [0xA326]!=3), and ``tick_mode9_boss`` in the mode-9 last-boss level ([0x2D8A]==9). Raises
    :class:`Pre2HybridGap` on a gated/unrecovered path (the boss-death finales, the camera state-6 finale).

    (object_tick + the 2nd pass run via their own bridges; they move onto NativeGameState as those adapters do.)
    """
    from pre2.recovered.object_spawn import GLYPH_LATCH, SONG_REQUEST

    def _apply(writes):
        song = writes.pop(SONG_REQUEST, None)             # [asm 7585: 7599-759C] boss-music load (02CC idx 0xD)
        if song is not None:
            state.song_request = song                     # consumed by NativeAudio.poll (file-free frame)
        glyph = writes.pop(GLYPH_LATCH, None)             # [asm 6BD3/6BD7] the mode-9 boss glyph for the renderer
        if glyph is not None:
            state.boss_glyph = glyph
        apply_ds(state, writes)

    rb, rw = readers(state)
    try:
        if rb(0x91FE) != 0xFF:                # [asm 6822/6827] camera active -> 70D7
            _apply(camera_engine(rb, rw, tile_reader(state)))
        if rb(0x2D8A) == 5 and (rb(0x8166) & 0xFE) == 0 and rw(0xA326) != 3:   # [asm 682C-6841] level-6 tree boss
            _apply(tick_level6_boss(rb, rw))              # 6D34 (incl. the recovered 94F3 death-burst finale)
        if rb(0x2D8A) == 9:                   # [asm 6844/6849] mode-9 last boss -> 6ADD
            _apply(tick_mode9_boss(rb, rw))
    except Pre2SpawnGap as exc:
        # The boss finales (94F3 death-burst, the camera state-6 boss-reach) and the lives-depleted death
        # (824D -> player_death -> the 4C69 game-over) are all recovered. The Pre2SpawnGap raises that remain are
        # DEFENSIVE guards on malformed/non-terminating data (a 94DC camera-offset table miss, a 7534/6B91 script
        # that never terminates) — states the original ASM would hang/garbage on, so we fail loud rather than
        # spin. Not expected in a normal playthrough.
        raise Pre2HybridGap(f"native object-spawn: {exc}") from exc


def native_object_system_step(state) -> None:
    """The whole 6822 object system over NativeGameState (no VM): the spawner branches (camera/boss), then
    object_tick (684E, in place), then the second pass (6913). Each sub-pass reads the previous one's writes
    in place — exactly the ASM's fall-through 6822 -> 684E -> 6913."""
    rb, rw = readers(state)
    native_object_spawn_step(state)                       # [asm 6822..6B..] camera_engine / tick_mode9_boss
    object_tick(LiveWalkerMem(_NativeCpuView(state)))     # [asm 684E..6912] per-slot walker, in place
    es = rw(0x2DDA)
    eb = (es << 4) & 0xFFFFF
    read_es = lambda o: state.data[(eb + (o & 0xFFFF)) & 0xFFFFF]   # noqa: E731 — level map (read-only)
    second_pass_tick(rb, rw, lambda w: apply_ds(state, w), read_es, rw(0x2DE4), rw(0x2DE6))   # [asm 6913..698B]


def native_trigger_scan(state) -> None:
    """[asm 52FE..5325] The per-frame position-trigger scan (the cave/teleport-entrance scan). When armed
    ([0x6BE1]!=0 and momentum dormant [0x6BC5]==0) it matches the player's tile coords (549A) against the 20-entry
    table at [0x8367] (stride 7, ``[+0]`` = source tile); a NO match touches nothing (byte-exact no-op). A match
    raises :class:`Pre2CaveTeleport` (BEFORE mutating anything) — the caller drives the multi-frame 5326
    transition via the ``native_cave_teleport`` generator (fade-out curtain, hidden pan, mini-pass, reveal,
    then the frame's remainder)."""
    rb, rw = readers(state)
    if rb(0x6BE1) != 0 and rb(0x6BC5) == 0:                  # [asm 5305 je / 530C jne] the trigger arm gate
        dx = _player_tile_coords(rw)                        # [asm 5313 -> 549A] the player's packed tile coord
        si = 0x8367                                         # [asm 5316]
        for _ in range(0x14):                               # [asm 5319 cx=0x14] the 20-entry table
            if rw(si) == dx:                                # [asm 531C je 5326] a match -> the cave teleport
                raise Pre2CaveTeleport(si)
            si = (si + 7) & 0xFFFF                          # [asm 5320 stride 7]
        # [asm 5325] no match -> ret (a byte-exact no-op)


def native_cave_teleport(state, si):
    """[asm 5326..53F5] The whole cave/teleport-entrance TRANSITION, as a generator (the runtime renders each
    yielded phase; a state-only consumer just drains it). The matched 20-byte-stride entry ``si`` gives the
    destination: ``[si+2]`` = camera (lo=X, hi=Y), ``[si+4]`` = player tile, ``[si+6]`` = the ``[0x6BD9]`` flag.

    Faithful sequence (verified against the VM on snapshot_pre2_20260702_105949):
      1. ``[asm 5326-532C]`` zero the velocities; ``[asm 5332]`` the 30C6 VERTICAL FADE-OUT curtain (the play
         area collapses to black, HUD stays) -> yields ``("fade", k)`` (VRAM-only; no DGROUP state).
      2. ``[asm 5335-535A]`` reset the scroll accumulator, snap the player to the destination tile; [0x8164] is
         saved and forced to 0xEC so the right-pan clamp can't block the pan ``[asm 533A/533F]``.
      3. ``[asm 5361-5387]`` the HIDDEN camera pan: Y steps first (3363 up / 33AD down, dl=0x10 = one full row
         per call), then X (3414 left / 3435 right) — 1 unit per step behind the black curtain, yielding
         ``("pan",)`` each step. The step primitives maintain the real scroll state ([0x2DE6]/[0x2DE4],
         [0x2DEA]/[0x2DE8], [0x2DBA], [0x6BC4]) exactly as the ASM.
      4. ``[asm 5389-5394]`` restore [0x8164]; [0x6BD9] = [si+6]; disarm [0x6BE1]. ``[asm 5399-53D5]`` the
         level-6 (inside-a-tree) boss re-init when [0x2D8A]==5.
      5. ``[asm 53D7-53F2]`` the arrival MINI-PASS: 35A1/3A27/3721 are render; the GAMEPLAY calls run natively —
         4907 (terrain), 8922 (projector), 6822 (object system), 26FA ([0x6BD5]++ + record mutation), 54AB
         (fireflies), 3922 (scroll counter). This is the extra tick the forward oracle used to see as the
         "one-tick drift" (demo 195135 @frame 206) — now byte-reproduced. 3054 is the CENTER-OUT REVEAL curtain
         -> yields ``("reveal", k)`` k=1..10 (the panel_copy strip pairs).
      6. The interrupted frame's REMAINDER (the post-0238 spine) — ``_frame_tail_after_trigger``."""
    rb, rw = readers(state)
    dest_cam = rw((si + 2) & 0xFFFF)                          # [si+2] destination camera (packed lo=X, hi=Y)
    dest_x, dest_y = dest_cam & 0xFF, (dest_cam >> 8) & 0xFF
    dest_tile = rw((si + 4) & 0xFFFF)                         # [si+4] destination player tile
    flag = rb((si + 6) & 0xFFFF)                              # [si+6] -> [0x6BD9]
    _ww(state, 0x4F22, 0)                                     # [asm 5326] Xvel = 0
    _ww(state, 0x4F2A, 0)                                     # [asm 532C] Yvel = 0
    for k in range(1, 10):                                    # [asm 5332] 30C6 vertical fade-out (VRAM-only)
        yield ("fade", k)
    _wb(state, 0x6BC4, 0)                                     # [asm 5335] vertical sub-tile accumulator
    saved_8164 = rw(0x8164)                                   # [asm 533A] push [0x8164]
    _ww(state, 0x8164, 0xEC)                                  # [asm 533F] pan clamp -> max (don't block the pan)
    _ww(state, 0x4F1C, (dest_tile & 0xFF) << 4)              # [asm 5350] player X = tile.lo << 4
    _ww(state, 0x4F1E, ((dest_tile >> 8) & 0xFF) << 4)       # [asm 535A] player Y = tile.hi << 4
    while rb(0x2DE6) != dest_y:                               # [asm 5361-5375] vertical pan first
        ok = _v_scroll_up(state, 0x10) if rb(0x2DE6) > dest_y else _v_scroll_down(state, 0x10)
        if not ok:                                            # the ASM would spin forever; we fail loud
            raise Pre2HybridGap(f"cave pan blocked vertically at [0x2DE6]={rb(0x2DE6)} dest={dest_y}")
        yield ("pan",)
    while rb(0x2DE4) != dest_x:                               # [asm 5377-5387] then horizontal
        ok = apply_camera_pan(state, "left" if rb(0x2DE4) > dest_x else "right")
        if not ok:
            raise Pre2HybridGap(f"cave pan blocked horizontally at [0x2DE4]={rb(0x2DE4)} dest={dest_x}")
        yield ("pan",)
    _ww(state, 0x8164, saved_8164)                            # [asm 538A] pop [0x8164]
    _wb(state, 0x6BD9, flag)                                 # [asm 5391]
    _wb(state, 0x6BE1, 0)                                     # [asm 5394] disarm the trigger
    if rb(0x2D8A) == 5:                                       # [asm 5399] level-6 (inside-a-tree) boss re-init
        _wb(state, 0x8166, rb(0x8166) & 1)                   # [asm 53A0]
        for off, val in ((0xA324, 0), (0xA325, 5), (0xA326, 0), (0xA328, 0), (0xA32A, 1),
                         (0xA329, 0), (0xA32B, 0x6E), (0xA32C, 8)):    # [asm 53A5-53CD]
            _wb(state, off, val)
        for k in range(0x69):                                # [asm 53CD-53D5] fill [0x5570..] with 0xFF
            _wb(state, 0x5570 + k, 0xFF)
    # [asm 53D7-53F2] the arrival mini-pass (35A1/3A27/3721 = render; the gameplay calls run natively):
    _apply_bytes(state, tick_terrain_entities(rw, rb, tile_reader(state)))   # [asm 53DD] 4907
    apply_ds(state, project_particles(rb, rw))                # [asm 53E0] 8922
    native_object_system_step(state)                          # [asm 53E3] 6822
    _ww(state, 0x6BD5, (rw(0x6BD5) + 1) & 0xFFFF)             # [asm 53E6: 26FA] inc word [0x6bd5]
    native_object_render_state(state)                         # [asm 53E6: 26FA] record mutation (life/flags)
    native_firefly_step(state)                                # [asm 53EC] 54AB
    native_scroll_script(state)                               # [asm 53EF] 3922
    for k in range(1, 11):                                    # [asm 53F2] 3054 center-out reveal (VRAM-only)
        yield ("reveal", k)
    _frame_tail_after_trigger(state)                          # the interrupted frame's remainder (023B..026D)


def _player_tile_coords(rw) -> int:
    """[asm 549A] dx = (sar(player_Y,4) & 0xFF) << 8 | (sar(player_X,4) & 0xFF) — the player's packed tile coord."""
    def _sar4(v: int) -> int:
        v &= 0xFFFF
        if v & 0x8000:
            v -= 0x10000
        return (v >> 4) & 0xFF
    return ((_sar4(rw(0x4F1E)) << 8) | _sar4(rw(0x4F1C))) & 0xFFFF


def native_proximity_trigger(state) -> None:
    """[asm 53F6..5497] The per-frame proximity-trigger scan (breakable-wall / opening-passage triggers): 15
    entries at [0x83F3] (stride 0xA), field [+4] = a packed tile coord (0xFFFF inactive, 0xFFFE already-fired).
    When the player comes within 8 (packed) of an armed entry it FIRES ([+4]=0xFFFE, camera shake [0x6BEA]=7); a
    fired entry re-applies its map modification (native_proximity_mapmod) each frame. No trigger in range and
    none fired -> a byte-exact no-op."""
    rb, rw = readers(state)
    dx = _player_tile_coords(rw)
    si = 0x83F3
    for _ in range(0xF):
        entry = rw((si + 4) & 0xFFFF)
        if entry == 0xFFFE:                                       # [asm 5407 je 5427] already fired -> the map mod
            native_proximity_mapmod(state, si)
        elif entry != 0xFFFF and ((dx - entry) & 0xFFFF) <= 8:   # [asm 540C-5413 jbe 541B] in range -> FIRST fire
            _ww(state, (si + 4) & 0xFFFF, 0xFFFE)                # [asm 541B] mark fired
            _wb(state, 0x6BEA, 7)                                # [asm 5420] camera shake
        si = (si + 0xA) & 0xFFFF


def native_proximity_mapmod(state, si) -> None:
    """[asm 5427..5497] The fired-trigger map modification (a wall rising / passage opening): every 4th frame
    ([0x6BD5]&3==0), shift the entry's ``height``x``width`` tile block ([si+3]x[si+2]) UP one row in the level map
    (es=[0x2DDA]), move the block anchor [si] up (-=0x100), reveal a fresh bottom row from the level asset
    ([0x2875]:[si+6]), advance the source ([si+6]-=width), and count down [si+8] — disarming ([si+4]=0xFFFF) at 0.
    The per-tile 653D re-blit is a render side-effect (the faithful renderer redraws the changed tiles)."""
    rb, rw = readers(state)
    _wb(state, 0x6BEA, 7)                                        # [asm 5429] camera shake
    if (rb(0x6BD5) & 3) != 0:                                    # [asm 542E] only acts every 4th frame
        return
    eb = (rw(0x2DDA) << 4) & 0xFFFFF                             # [asm 5435] level-map segment
    sb = (rw(0x2875) << 4) & 0xFFFFF                             # [asm 5471] level-asset (reveal source) segment
    width = rb((si + 2) & 0xFFFF)                                # [asm 5439]
    height = rb((si + 3) & 0xFFFF)                               # [asm 543B]
    di = (rw(si) - 0x100) & 0xFFFF                               # [asm 543F/5445] block anchor, one row up
    for _row in range(height):                                   # [asm 5449-5463] shift the block up one row
        for _col in range(width):
            state.data[(eb + di) & 0xFFFFF] = state.data[(eb + ((di + 0x100) & 0xFFFF)) & 0xFFFFF]   # [asm 544B-5450]
            di = (di + 1) & 0xFFFF
        di = (di - width + 0x100) & 0xFFFF                       # [asm 545B-545D] next row
    _ww(state, si, (rw(si) - 0x100) & 0xFFFF)                    # [asm 5465] [si] -= 0x100
    bx = rw((si + 6) & 0xFFFF)                                   # [asm 5469] source pointer
    for _col in range(width):                                    # [asm 5471-5483] reveal a fresh bottom row
        state.data[(eb + di) & 0xFFFFF] = state.data[(sb + bx) & 0xFFFFF]
        di = (di + 1) & 0xFFFF
        bx = (bx + 1) & 0xFFFF
    _ww(state, (si + 6) & 0xFFFF, (rw((si + 6) & 0xFFFF) - width) & 0xFFFF)   # [asm 5488] [si+6] -= width
    cnt = (rb((si + 8) & 0xFFFF) - 1) & 0xFF                     # [asm 548B] countdown
    _wb(state, (si + 8) & 0xFFFF, cnt)
    if cnt == 0:                                                 # [asm 548E]
        _ww(state, (si + 4) & 0xFFFF, 0xFFFF)                    # [asm 5490] done -> disarm


_DS_BASE = DATA_SEG << 4


def _wb(state, off: int, v: int) -> None:
    state.data[(_DS_BASE + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF


def _ww(state, off: int, v: int) -> None:
    _wb(state, off, v)
    _wb(state, off + 1, v >> 8)


_CS_BASE = 0x1030 << 4
_TIMER_PHASE = _CS_BASE + 0x1D6B            # cs:[0x1d6b]: the timer ISR's mod-4 tick phase (07B2/07B7)
_TICKS_PER_FRAME = 3                         # the main loop waits 3 VGA retraces (0264) -> 3 timer ticks / frame


def native_idle_timer_tick(state, ticks: int = _TICKS_PER_FRAME) -> None:
    """[asm 0264 -> 17C0] Advance the wall-clock idle counter ``[0x27F0]`` the way the timer ISR does.

    ``[0x27F0]`` (a free-running 32-bit counter at ``[0x27F0]``/``[0x27F2]``) is bumped by the timer ISR
    (``1030:17C9 add`` / ``17CE adc``) every 4th tick — i.e. whenever the mod-4 phase ``cs:[0x1d6b]`` wraps to 0
    (``07B2 inc`` / ``07B7 and 3`` / ``07BD je 17C0``). ``ticks`` = how many timer ticks fire this frame; it sits
    AFTER the player step ``022F`` — the only gameplay reader, the idle-fidget selector at ``5DC9`` using
    ``[0x27F0] & 0x1FF`` — so the player reads the frame-START value and this frame's ticks land at frame END.

    CAVEAT (measured): ``ticks`` is only NOMINALLY the 3-retrace wait (``44FB`` @ ``0264``); the PIT is NOT
    retrace-locked, so the VM actually fires a VARIABLE, INSTRUCTION-COUNT-driven number per frame — ~**8** in busy
    L1 gameplay (measured 4..11 via the ``cs:[0x1d67]`` raw tick counter), ~**1** in the per-retrace front-end.
    The VM-less core has no instruction count, so no fixed ``ticks`` is byte-exact across scenes; ``[0x27F0]`` is
    therefore EXCLUDED from the forward verify. Its only downstream reader is the idle-fidget pose, which drifts
    (triggers a few frames off) only after a LONG stationary idle — cosmetic, and re-arms the instant the player
    moves. Getting ``[0x27F0]`` non-zero at level start still matters: at 0 the idle player picks the wrong fidget
    (a crouch instead of the upright stand), so the front-end must advance it. Default ``ticks`` keeps the SHORT-idle
    common case right; long-idle byte-exactness is unattainable VM-less (see [[pre2-native-render-state]])."""
    d = state.data
    phase = d[_TIMER_PHASE]
    lo = d[_DS_BASE + 0x27F0] | (d[_DS_BASE + 0x27F1] << 8)
    hi = d[_DS_BASE + 0x27F2] | (d[_DS_BASE + 0x27F3] << 8)
    for _ in range(ticks):
        phase = (phase + 1) & 3                              # [asm 07B2/07B7] inc [0x1d6b]; and 3
        if phase == 0:                                       # [asm 07BD je 17C0]
            lo = (lo + 1) & 0xFFFF                           # [asm 17C9] add word [0x27f0], 1
            if lo == 0:
                hi = (hi + 1) & 0xFFFF                       # [asm 17CE] adc word [0x27f2], 0
    d[_TIMER_PHASE] = phase
    _ww(state, 0x27F0, lo)
    _ww(state, 0x27F2, hi)


def native_firefly_step(state) -> None:
    """[asm 0253: 54AB] Step the firefly swarm (animation + both RNGs) in place — the per-frame sim. The VRAM
    draw is the renderer's job; only the state contract (slots + RNG seeds + scratch) is applied here."""
    from pre2.bridge.firefly_sim import read_firefly_sim_state, write_firefly_sim_state
    from pre2.recovered.firefly_sim import step_fireflies
    st = read_firefly_sim_state(state)
    step_fireflies(st)
    write_firefly_sim_state(state, st)


def native_scroll_script(state) -> None:
    """[asm 0256: 3922] The scripted-camera-scroll STATE update. Advances the frame counter [0x2dbe] and, for a
    level with an active script (only LEVELG / index 0x0F, a hidden auto-scroll bonus stage), ramps the vertical
    scroll amount [0x6bf6] through the per-level table at [0x2dbc]. Verified byte-exact vs the ASM 3922 state half
    (pre2/recovered/scroll_script.py). A no-script level is just the counter inc.

    The render half (3922:396A.. — the LEVELG falling snow) is reproduced too: it OR-plots white pixels onto the
    draw page AND advances the flake array + the shared gameplay rng, so running it here keeps BOTH byte-exact.
    The plotted pixels are stashed on ``state.snow_plots`` for the faithful renderer to overlay; they are
    ``(page_relative_byte_offset, bit_mask)`` pairs (empty when the wind is zero).

    State access goes through a human-named ``ScrollScriptView`` (the byte-backed layout bridge) — the logic is
    offset-free; the view is the one place this island's DGROUP offsets live."""
    from pre2.bridge.dgroup_view import ScrollScriptView
    from pre2.recovered.scroll_script import scroll_script_snow, scroll_script_state
    view = ScrollScriptView(state)
    scroll_script_state(view)
    state.snow_plots = scroll_script_snow(view)


def native_level_state(state) -> None:
    """[asm 0259: 4C69] The per-frame level/death state dispatcher. Idle (mode [0x6be6], respawn [0x6be4], death
    [0x6be5] all 0) it returns no-carry and the loop continues. Armed:
      * [0x6be4]==1 -> the respawn-to-checkpoint handler ``native_4f6c``, raised as a ``Pre2RespawnTransition``:
        the death-bounce is a 60-frame ANIMATION, so it must be driven OUTSIDE the single-frame loop (by the
        runtime/flow driver, rendering each frame) — running it blocking here would teleport the player to the
        checkpoint with no animation. The boss hit set [0x6be4]=2 (8295/65b3); the player step counts it 2->1->0;
      * [0x6be4]!=0 (i.e. ==2) -> idle this frame (4C69 dispatches nothing while the respawn counter is winding);
      * [0x6be5]==1 death -> game-over restart (5063) / ==0xff GAME-COMPLETE -> THE END (5034) / [0x6be6]
        level-end (4F65) -> the carry paths that return to main's level change at 0x12f."""
    rb, _ = readers(state)
    if rb(0x6BE6) != 0:                                           # ==1 normal level-end, >1 the 4C74 warp — both
        raise Pre2LevelEndTransition()                            # [asm 4cba/4c74] a transition; native_level_end
        #                                                          reads [0x6be6] + the [0x2cf6] table to pick the level
    if rb(0x6BE4) == 1:
        raise Pre2RespawnTransition()                              # [asm 4f6c] respawn — a multi-frame transition
    if rb(0x6BE4) != 0:
        return                                                      # [0x6be4]==2: 4C69 idle (counter winding down)
    if rb(0x6BE5) == 1:
        raise Pre2GameOverTransition()                            # [asm 5063] death -> game-over restart (level 1)
    if rb(0x6BE5) == 0xFF:
        raise Pre2GameComplete()                                  # [asm 5034] THE END (level 0xE cleared)


def native_respawn_gate(state) -> None:
    """[asm 0261: 45AF] The respawn-animation pass: when respawning ([0x6be4]!=0) it DRAWS the death/respawn
    sequence (reading [0x6c0e]/[0x6c10]) — pure render, no gameplay-state writes — so it is a no-op for the
    gameplay step (the faithful renderer draws it). [verified render: the whole-loop verify stays DIV=0]"""
    return


def native_special_event(state) -> None:
    """[asm 026A: 67D7] The BONUS-letters event. When all 5 letters are collected ([0x6CA7]==0x1F), spawn the
    reward sprite (0x6E) at the player (Y-0x70) via 8D1B, reset the mask, and arm [0x6BFF]/[0x6C00]. Otherwise, if
    the [0x6CA8] 0x38-bit group is complete, clear it and set [0x6BE2]=0x294. Neither armed -> a byte-exact no-op."""
    from pre2.recovered.combat_interaction import spawn_effect_burst
    rb, rw = readers(state)
    if rb(0x6CA7) == 0x1F:                                   # [asm 67D7] all 5 BONUS letters -> reward burst
        _ww(state, 0xA336, rw(0x4F1C))                      # [asm 67DE] burst pos X = player X
        _ww(state, 0xA338, (rw(0x4F1E) - 0x70) & 0xFFFF)    # [asm 67E4] pos Y = player Y - 0x70
        _ww(state, 0xA33A, 0x6E)                            # [asm 67ED] reward sprite id
        apply_ds(state, spawn_effect_burst(rb, rw, 0, 0, 1))   # [asm 67FA] 8D1B: spawn 1
        _wb(state, 0x6CA7, 0)                               # [asm 67FD] reset the letters mask
        _wb(state, 0x6BFF, 1)                              # [asm 6802]
        _wb(state, 0x6C00, 0x2C)                           # [asm 6807]
    elif (rb(0x6CA8) & 0x38) == 0x38:                       # [asm 680D-6814] the [0x6CA8] 0x38-group is complete
        _wb(state, 0x6CA8, rb(0x6CA8) & 0xC7)              # [asm 6816] clear those bits
        _ww(state, 0x6BE2, 0x294)                          # [asm 681B]


def native_camera_shake(state) -> None:
    """[asm 026D: 4C30] One frame's screen-shake apply: from magnitude [0x6BEA] + parity [0x6BD5]&1, write the
    renderer row-bias [0x6BF8], the jittered magnitude, and the odd-frame horizontal nudge [0x4F1E]-=3.
    Magnitude 0 -> no change (no shake). [recovered leaf]"""
    from pre2.recovered.camera_shake import apply_camera_shake
    rb, rw = readers(state)
    res = apply_camera_shake(rw(0x6BF8), rb(0x6BEA), rb(0x6BD5), rb(0x4F27), rw(0x4F1E))
    _ww(state, 0x6BF8, res.row_factor)
    _wb(state, 0x6BEA, res.magnitude)
    _ww(state, 0x4F1E, res.h_scroll)


_COMBAT_SLOTS_LO = 0x4F2E     # [asm 88D7] first thrown-weapon slot
_COMBAT_STRIDE = 0x12
_COMBAT_N = 4
_PLAYER_SRC = 0x4F0A          # [asm 88FC] the player's collision sprite
_COMBAT_FLAG = 0xA312         # [asm 88DD] full-tolerance flag (read by hitbox_overlap 8D7B)
_SCRIPTED_POSE = 0x6BC5       # [asm 88F5] scripted-pose gate (skips the player pass)
_PLAYER_YVEL = 0x4F2A         # [asm 890F/8916] player Yvel (bounce on a hit/collect)


def _combat_source_pass(state, si, *, bounce: bool) -> None:
    """[asm 8C21 then 899E] Resolve one source sprite (a projectile or the player at ``si``) vs enemies then bonus
    tiles, applying each result to the live state IN PLACE so the bonus scan sees the projectile's writes (the
    ASM's fall-through). ``bounce`` (player only) fires the [0x4f2a] Yvel bounce on an enemy hit OR a collect."""
    from pre2.native.audio import native_emit_sfx
    from pre2.recovered.combat_interaction import bonus_pickup_scan, projectile_vs_enemies
    rb, rw = readers(state)
    writes, sfx, hit, _slot = projectile_vs_enemies(rb, rw, si)       # [asm 8C21] source-vs-ENEMY
    _apply_bytes(state, writes)
    native_emit_sfx(state, sfx)                                       # emit the kill sound (play_sfx 2)
    did = hit                                                        # [asm 88EB/8908] jb -> skip the bonus scan
    if not hit:                                                      # CF=0 -> source-vs-BONUS pickup
        ds, mapw, _redraws, collected = bonus_pickup_scan(rb, rw, si)   # [asm 899E]
        _apply_bytes(state, ds)
        if mapw:                                                     # the collected tiles' level-map rewrites (es=[0x2DDA])
            eb = (rw(0x2DDA) << 4) & 0xFFFFF
            for off, (val, width) in mapw.items():
                state.data[(eb + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF
                if width == 2:
                    state.data[(eb + ((off + 1) & 0xFFFF)) & 0xFFFFF] = (val >> 8) & 0xFF
        # _redraws = the on-screen tile re-blit (a render side-effect) — the faithful renderer's job.
        did = collected                                             # [asm 890D] jae -> skip the bounce if no collect
    if bounce and did and rw(_PLAYER_YVEL) != 0:                     # [asm 890F/8914] player Yvel != 0
        _ww(state, _PLAYER_YVEL, 0xFFB0)                            # [asm 8916] bounce up (-0x50)


def native_combat_pass(state) -> None:
    """[asm 88D7] The per-frame COMBAT / pickup pass: the 4 thrown-weapon slots ([0x4F2E]) then the player
    ([0x4F0A]), each resolved vs enemies (8C21) then bonus tiles (899E). Fills the effect/debris burst pools
    ([0x50A8]/[0x5450]), damages/kills enemies, consumes hit projectiles, collects secret/bonus tiles, and
    bounces the player ([0x4f2a]) on a hit/collect. GAMEPLAY-coupled (feeds the effect pools + enemy state read
    back next frame) — the render classification wrongly skipped it, so native forward-diverged at the first hit."""
    rb, rw = readers(state)
    _wb(state, _COMBAT_FLAG, 1)                                      # [asm 88DD] [0xA312] = 1 (relax the bounce test)
    for k in range(_COMBAT_N):                                       # [asm 88D7/88DA] the 4 projectile slots
        si = (_COMBAT_SLOTS_LO + k * _COMBAT_STRIDE) & 0xFFFF
        if rw((si + 4) & 0xFFFF) != 0xFFFF:                          # [asm 88E2] slot occupied?
            _combat_source_pass(state, si, bounce=False)
    if rb(_SCRIPTED_POSE) == 0:                                      # [asm 88F5] not a scripted pose
        if rw((_PLAYER_SRC + 4) & 0xFFFF) != 0xFFFF:                # [asm 88FF] the player sprite present?
            _combat_source_pass(state, _PLAYER_SRC, bounce=True)
    _wb(state, _COMBAT_FLAG, 0)                                      # [asm 891C] [0xA312] = 0


def native_particle_consume(state) -> None:
    """[asm 4B8E state half] The point-particle pass (spider-threads / fireflies / sparkles at [0x7DE6], 20 slots
    of 6 bytes) advances each active slot by its angle/speed, plots one pixel, then KILLS the slot — so the array
    is one-shot, gone by the next frame. The plot is render (the faithful renderer redraws it from a 4B8E-entry
    snapshot); the STATE half — write the advanced Y back to [slot+2] and the kill sentinel [slot]=0xFFFF — is
    gameplay-coupled (the effect-spawn handlers reuse a freed slot), so native must run it or the array fills up."""
    from pre2.bridge.particles import apply_particle_writeback, read_particle_consume_inputs
    from pre2.recovered.particles import advance_particle
    slots, _cc, _cr, _yb, _pg, cos, sin = read_particle_consume_inputs(state)
    if slots:                                                          # [asm 4BA9] active slots (X != 0xFFFF)
        wb = [(i, advance_particle(x, y, a, s, cos, sin)[1]) for (i, x, y, a, s) in slots]   # [asm 4BC2-4BD2] ny
        apply_particle_writeback(state, wb)                           # [asm 4BD2 -> 4C1D] [slot+2]=ny, [slot]=0xFFFF


def native_gameplay_frame(state) -> None:
    """Drive the WHOLE per-frame main loop (0214..0270) over NativeGameState — VM-less, in spine order. The
    recovered gameplay systems run; the render calls (88D7, 8922, and the 3668/35A1/3A27/4B8E/26FA/3721/6772
    cluster) are the faithful renderer's job and are not run here. The event-driven paths a normal frame doesn't
    take — death/respawn + the level-state machine (4C69/45AF), the scripted camera (3922), the 67D7 one-shot —
    are byte-exact no-ops when idle and fail loud when armed (witnessed by the death/game-over demos), never a
    silent skip."""
    rb, rw = readers(state)
    apply_ds(state, tick_popup_ring(rw))                              # [asm 021A] 581E
    native_combat_pass(state)                                        # [asm 021D] 88D7 (combat/pickup pass)
    native_object_system_step(state)                                 # [asm 0220] 6822 (whole object system)
    apply_ds(state, tick_projectiles(rw, rb))                        # [asm 0223] 6210
    apply_ds(state, tick_particles(rw, rb, tile_reader(state)))      # [asm 0226] 60FE
    apply_ds(state, tick_debris_pool(rw))                            # [asm 0229] 60DF
    _apply_bytes(state, tick_terrain_entities(rw, rb, tile_reader(state)))   # [asm 022C] 4907 (byte-level)
    native_player_step(state)                                       # [asm 022F] 5850 (whole player update)
    native_player_interaction(state)                                # [asm 0232] 8295 (player<->world pass)
    apply_ds(state, project_particles(rb, rw))                      # [asm 0235] 8922 effect-sprite projector
    native_trigger_scan(state)                                      # [asm 0238] 52FE (raises Pre2CaveTeleport on a
    #   match — the caller drives native_cave_teleport, which finishes with _frame_tail_after_trigger itself)
    _frame_tail_after_trigger(state)                                # [asm 023B..026D] the frame's remainder


def _frame_tail_after_trigger(state) -> None:
    """The main-loop frame REMAINDER after the position-trigger scan ([asm 023B..026D]) — shared by the normal
    frame and the cave-teleport transition (whose 5326 runs mid-scan, then the frame continues here)."""
    rb, rw = readers(state)
    native_proximity_trigger(state)                                 # [asm 023B] 53F6 (proximity trigger; no-op unfired)
    native_camera_follow(state)                                     # [asm 023E] 5643 (H+V camera follow/scroll)
    # [asm 0241..0250] 3668/35A1/3A27/4B8E/26FA/3721 render cluster — the faithful renderer's job. Two gameplay
    # side effects are extracted: 4B8E's particle consume (state half below) and 26FA (object_render), which bumps
    # the free-running 16-bit frame counter [0x6bd5] that the animation phase reads ([0x6bd5]&1/&3/&7/&0xf) — in 11
    # gameplay calls. The prefix above already read it as N (frame start); the firefly/scroll/respawn/shake read N+1.
    from pre2.bridge.particles import read_particles
    state.particle_capture = read_particles(state)                  # snapshot [0x7DE6] at the 4B8E ENTRY (spider-
    #   threads/sparkles) so native_render can DRAW them — the consume below KILLS the one-shot slots
    native_particle_consume(state)                                  # [asm 0247: 4B8E state] advance-Y + kill [0x7DE6]
    _ww(state, 0x6BD5, (rw(0x6BD5) + 1) & 0xFFFF)                    # [asm 024D: 2708] inc word [0x6bd5]
    native_object_render_state(state)                               # [asm 024D: 26FA] record mutation (life/flags)
    native_firefly_step(state)                                      # [asm 0253] 54AB
    native_scroll_script(state)                                     # [asm 0256] 3922
    native_level_state(state)                                       # [asm 0259] 4C69 (carry -> level change @ 0x12f)
    native_respawn_gate(state)                                      # [asm 0261] 45AF
    # [asm 0264] 44FB (4509 + 1C65) render/timing helper; [asm 0267] 6772 — the light-fade pass. Its DAC ramp
    # is the renderer's job (native_apply_palette_fade), but its STATE half is tick-owned and runs here.
    # The 44FB 3-retrace WAIT is where the 70Hz timer fires 3× -> advance the [0x27F0] idle counter (fidget anim).
    native_idle_timer_tick(state)                                   # [asm 0264: 44FB wait -> 17C0 timer]
    native_light_fade_step(state)                                   # [asm 0267: 6772 state] [0x6C03]++ / flag clear
    native_special_event(state)                                     # [asm 026A] 67D7
    native_camera_shake(state)                                      # [asm 026D] 4C30
    # [asm 0270] jmp 0214 — loop back


def native_light_fade_step(state) -> None:
    """[asm 6772 STATE half, called at 0267 each main-loop tick] The light-fade (dark-cave lamp) progress:
    while a fade is active ([0x6C01]=to-dark | [0x6C02]=to-level-palette, set by the light pickups in
    player_interaction 876C/8790) the ASM increments the step [0x6C03] ([asm 677B]) and, when every one of the
    0x30 DAC channels is within one step of its target ([asm 67C8-67D1], anim count == 0), clears both flags —
    fade complete. The DAC ramp itself is render (native_apply_palette_fade reads the step WITHOUT mutating).
    Inactive fade -> byte-exact no-op. Splitting the pass this way keeps the tick-owned bytes ([0x6C01/02/03])
    in the gameplay frame — found by the safe-hooks demo 230900 (tick 382: the VM counted [0x6C03] 1,2,4...
    while state-only native stayed 0), and it also fixes the native fade PACING (it advanced per RENDER call,
    i.e. --fps-dependent, instead of per game tick)."""
    d = state.data
    base = DATA_SEG << 4
    if d[base + 0x6C01] == 0 and d[base + 0x6C02] == 0:              # [asm 6771/6775 je 67D6] no active fade
        return
    step = (d[base + 0x6C03] + 1) & 0xFF                             # [asm 677B] inc byte [0x6C03]
    d[base + 0x6C03] = step
    level = d[base + 0x2D8A]
    lvl_pal = d[base + 0x2D00 + level * 2] | (d[base + 0x2D00 + level * 2 + 1] << 8)   # [asm 677F-6787]
    s_off, b_off = lvl_pal, 0xACB7                                   # [asm 6787/6791] src=level, dst=dark
    if d[base + 0x6C02]:                                             # [asm 6799-67A0] fading BACK -> swap
        s_off, b_off = b_off, s_off
    anim = 0
    for k in range(0x30):                                            # [asm 67A2-67C6] 16 colours x RGB
        s = d[base + ((s_off + k) & 0xFFFF)]
        b = d[base + ((b_off + k) & 0xFFFF)]
        if abs(s - b) > step:                                        # [asm 67B3 ja] still ramping
            anim += 1
    if anim == 0:                                                    # [asm 67C8-67D1] complete -> clear flags
        d[base + 0x6C01] = 0
        d[base + 0x6C02] = 0


def native_object_render_state(state) -> None:
    """[asm 26FA state half] The moving-sprite renderer's RECORD MUTATION: for every active slot (sprite_id
    [+4] != 0xFFFF) in the list 0x4F0A..0x5720 it decrements the life countdown [+0x11] (saturating) and updates
    the drawn flag [+5]. This is the GAMEPLAY-COUPLED half of object_render (the pixel half is native_render's):
    skipping it left the sprite lives frozen, which cascades into the effect-pool free + the combat effect-spawn
    (the forward-verify divergence). [0x6bd5] was already incremented by the caller (the extracted 26FA tick), so
    read the camera with frame_pre_inc=False. Composes the SAME recovered leaves the object_render checkpoint
    verifies (plan_sprite -> drawn, plan_record_update, write_record)."""
    cam = _obj_render.read_camera(state, frame_pre_inc=False)
    flash: list[int] = []
    for off, spr in _obj_render.read_active_list(state):
        if spr.sprite_id == 0xFFFF:                                 # [asm 2713] empty slot
            continue
        # [asm 2757/28BA] the OPAQUE/flash flag (id bit14 = 0x4000) is READ here to pick the solid-white silhouette,
        # then plan_record_update CLEARS it (& 0xBF) — a one-frame flash. Capture the slot so native_render can
        # re-apply it (the fresh render at the commit boundary sees the already-cleared record otherwise).
        if spr.sprite_id & 0x4000:
            flash.append(off)
        draw = plan_sprite(spr, _obj_render.read_attr(state, spr.sprite_id), cam)   # the draw/visibility decision
        _obj_render.write_record(state, off, plan_record_update(spr, draw is not None))  # [asm 2732/2742/28B6]
    state.flash_slots = flash or None


def native_death_bounce_509d(state):
    """[asm 509D] The death-bounce animation — the player's death-jump (called first by the respawn 4F6C and the
    death 5063). Sets the player flying (Yvel=+0xF, anim 0x21) drifting toward the screen centre (Xvel=+/-5), then
    runs 0x3C=60 frames of the entity-update SUBSET (581E/6822/6210/60FE/60DF + the 26FA frame-counter tick +
    54AB/3922) while integrating the player ballistically with gravity (NO collision — the corpse arcs up then
    falls through the level). The render cluster (3668..3721, 44FB) is the renderer's job.

    This is a GENERATOR: it ``yield``s once per bounce frame (at the loop top, mirroring the ASM's 0x50de) so the
    caller renders each of the 60 frames — the whole arc animates instead of teleporting to the end. The ASM's
    509d is an inner render-loop within one main-loop iteration; here the runtime/flow-driver pumps the renderer
    between yields. Drive to completion (``for _ in native_death_bounce_509d(state): ...``) to apply all 60 frames.

    Verified: the player's death-bounce TRAJECTORY is byte-exact vs the ASM (timer-driven synthetic invoke — the
    render busy-waits need the full timing machinery, so it's driven through play._advance_demo_frame). The only
    residual is the 8 effect slots that the render 26FA frees ([slot+4]=0xffff) as their lifetime expires —
    render-managed, and in the full 4F6C respawn it is immediately wiped by 5237's pool re-init, so it is
    irrelevant to the respawn outcome (and would be reproduced by native_render's per-frame draw in a renderer)."""
    def _s16(v):
        return v - 0x10000 if v & 0x8000 else v

    rb, rw = readers(state)
    from pre2.native.audio import native_play_sfx
    native_play_sfx(state, 7)                                        # [asm 50a6-50a9] the death SCREAM (play_sfx 7)
    _wb(state, 0x4F2D, 0)                                            # [asm 50ac] clear the player death-state byte
    _ww(state, 0x4F20, 0x21)                                        # [asm 50b1] death anim frame
    _ww(state, 0x4F2A, 0x0F)                                        # [asm 50b7] Yvel = +15 (the upward kick)
    centre = ((rw(0x2DE4) + 0xA) << 4) & 0xFFFF                     # [asm 50c0-50cd] (camera cell + 10 tiles) * 16
    _ww(state, 0x4F22, 5 if rw(0x4F1C) < centre else (-5 & 0xFFFF))  # [asm 50cf-50d8] drift toward screen centre
    for _ in range(0x3C):                                           # [asm 50db cx=0x3c] 60 frames
        yield                                                       # render THIS bounce frame (mirrors the ASM
        #   loop-top 0x50de: the caller renders the corpse mid-arc, then we advance one frame of physics)
        _ww(state, 0x4F0E, 0xFFFF)                                  # [asm 50df] suppress the normal player render
        apply_ds(state, tick_popup_ring(rw))                        # [asm 50e5] 581E
        native_object_system_step(state)                           # [asm 50e8] 6822
        apply_ds(state, tick_projectiles(rw, rb))                  # [asm 50eb] 6210
        apply_ds(state, tick_particles(rw, rb, tile_reader(state)))  # [asm 50ee] 60FE
        apply_ds(state, tick_debris_pool(rw))                      # [asm 50f1] 60DF
        # [asm 50f4-5103] 3668/35A1/3A27/4B8E/26FA/3721. 4B8E's STATE half is GAMEPLAY (advance-Y + KILL expired
        # [0x7DE6] particles): the bounce's 6822 SPAWNS point-effects (an orbit object emits 3/frame via 7FD9), so
        # without the per-frame kill the 20-slot [0x7DE6] list fills over the 60 bounce frames and find_free (8014,
        # unbounded) walks off the end into the [0x9203] backup — corrupting it, which the respawn's 5251 restore
        # then propagates into the working tables (the tick-1299 divergence, demo 175517). Run it like 509d does.
        native_particle_consume(state)                            # [asm 50fx] 4B8E state (advance-Y + kill [0x7DE6])
        _ww(state, 0x6BD5, (rw(0x6BD5) + 1) & 0xFFFF)              # [asm 26fa:2708]
        native_firefly_step(state)                                 # [asm 5106] 54AB
        native_scroll_script(state)                                # [asm 5109] 3922
        # [asm 510c] 44FB render/timing helper
        _ww(state, 0x4F1C, (rw(0x4F1C) + rw(0x4F22)) & 0xFFFF)      # [asm 510f] X += Xvel
        yv = _s16(rw(0x4F2A)) - 1                                   # [asm 5116] Yvel -= 1 (gravity)
        if yv < -0x10:                                             # [asm 511a] clamp at terminal -0x10
            yv = -0x10
        _ww(state, 0x4F2A, yv & 0xFFFF)                            # [asm 5122]
        _ww(state, 0x4F1E, (rw(0x4F1E) - yv) & 0xFFFF)            # [asm 5125] Y -= Yvel
    # [asm 512c] call 30c6 — camera/scroll fixup (the renderer's job; no gameplay DGROUP state)
