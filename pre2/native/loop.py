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
from pre2.checkpoints.common import Pre2HybridGap
from pre2.recovered.effects_update import (tick_debris_pool, tick_particles, tick_popup_ring,
                                           tick_projectiles)
from pre2.recovered.object_inject import second_pass_tick
from pre2.recovered.object_spawn import Pre2SpawnGap, camera_engine, tick_mode9_boss
from pre2.recovered.object_tick import object_tick
from pre2.recovered.terrain_entities import tick_terrain_entities
from pre2.native.state import DATA_SEG
from pre2.native.camera_scroll import native_camera_follow
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
    (0x88D7, "render", "player throwable-weapons draw"),
    (0x6822, "native", "object system: camera_engine/tick_mode9_boss -> object_tick(684E) -> 2nd pass(6913)"),
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
    (0x8922, "render", "project_particles (effect-sprite projector -> render slots)"),
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
    (0x4B8E, "render", "particles_draw"),
    (0x26FA, "render", "moving-sprite renderer (object_render) -> faithful renderer"),
    (0x3721, "render", "foreground-tile pass -> faithful renderer"),
    (0x54AB, "native", "native_firefly_step (swarm sim + RNG)"),
    (0x3922, "native", "native_scroll_script (counter inc; scripted camera fails loud)"),
    (0x4C69, "native", "native_level_state (idle no-op; death/respawn/level-end armed fails loud)"),
    (0x45AF, "native", "native_respawn_gate (idle no-op; respawn animation fails loud)"),
    (0x44FB, "render", "4509+1C65 render/timing helper"),
    (0x6772, "render", "render-frame commit (-> faithful renderer)"),
    (0x67D7, "native", "native_special_event ([0x6ca7]==0x1f one-shot fails loud)"),
    (0x4C30, "native", "native_camera_shake"),
]


def spine_coverage() -> Counter:
    """The VM-less roadmap as counts: how many main-loop calls are native / render / still gaps."""
    return Counter(kind for _, kind, _ in MAIN_LOOP_SPINE)


def native_object_spawn_step(state) -> None:
    """Run the recovered 6822 spawner branches over the :class:`NativeGameState` — the first native-driven
    main-loop call, no VM. Mirrors the 6822 dispatch: ``camera_engine`` when the camera is active
    ([0x91FE]!=0xFF) and ``tick_mode9_boss`` in the mode-9 last-boss level ([0x2D8A]==9). Raises
    :class:`Pre2HybridGap` on a gated/unrecovered path (the boss-death finale, the camera state-6 finale).

    (object_tick + the 2nd pass run via their own bridges; they move onto NativeGameState as those adapters do.)
    """
    rb, rw = readers(state)
    try:
        if rb(0x91FE) != 0xFF:                # [asm 6822/6827] camera active -> 70D7
            apply_ds(state, camera_engine(rb, rw, tile_reader(state)))
        if rb(0x2D8A) == 9:                   # [asm 6844/6849] mode-9 last boss -> 6ADD
            apply_ds(state, tick_mode9_boss(rb, rw))
    except Pre2SpawnGap as exc:
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
    """[asm 52FE..5325] The per-frame position-trigger scan. When armed ([0x6BE1]!=0 and momentum dormant
    [0x6BC5]==0) it matches the player's tile coords (549A) against the 20-entry table at [0x8367] and, on a hit,
    teleports the player (5326+: zero velocity, reposition, reset camera). Unarmed it touches nothing — a
    byte-exact no-op (witnessed: never armed across 4 demos / 1261 calls). The armed scan + teleport are
    unrecovered (no witness), so an armed frame fails loud rather than guessing."""
    rb, _ = readers(state)
    if rb(0x6BE1) != 0 and rb(0x6BC5) == 0:                  # [asm 5305 je / 530C jne] the trigger arm gate
        raise Pre2HybridGap("native trigger 0x52FE armed ([0x6BE1]!=0) — position-trigger scan/teleport not recovered")


def _player_tile_coords(rw) -> int:
    """[asm 549A] dx = (sar(player_Y,4) & 0xFF) << 8 | (sar(player_X,4) & 0xFF) — the player's packed tile coord."""
    def _sar4(v: int) -> int:
        v &= 0xFFFF
        if v & 0x8000:
            v -= 0x10000
        return (v >> 4) & 0xFF
    return ((_sar4(rw(0x4F1E)) << 8) | _sar4(rw(0x4F1C))) & 0xFFFF


def native_proximity_trigger(state) -> None:
    """[asm 53F6..541A] The per-frame proximity-trigger scan: 15 entries at [0x83F3] (stride 0xA), field [+4] =
    a trigger's packed tile coord (0xFFFF inactive, 0xFFFE already-fired). When the player comes within 8 of an
    armed entry it fires (-> a map modification via 653D); a fired entry re-applies its tile mod each frame. With
    no trigger in range and none fired the scan writes nothing — a byte-exact no-op (witnessed never fires across
    4 demos / 1261 calls). The map-modifying fire path (5427+/653D) is unrecovered, so a firing frame fails loud."""
    _, rw = readers(state)
    dx = _player_tile_coords(rw)
    si = 0x83F3
    for _ in range(0xF):
        entry = rw((si + 4) & 0xFFFF)
        if entry == 0xFFFE:                                       # [asm 5407 je 5427] already fired -> map mod
            raise Pre2HybridGap("native trigger 0x53F6: a proximity trigger has fired (653D map-mod) — not recovered")
        if entry != 0xFFFF and ((dx - entry) & 0xFFFF) <= 8:     # [asm 540C-5413 jbe 541B] player in range -> fire
            raise Pre2HybridGap("native trigger 0x53F6: player fired a proximity trigger (653D map-mod) — not recovered")
        si = (si + 0xA) & 0xFFFF


_DS_BASE = DATA_SEG << 4


def _wb(state, off: int, v: int) -> None:
    state.data[(_DS_BASE + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF


def _ww(state, off: int, v: int) -> None:
    _wb(state, off, v)
    _wb(state, off + 1, v >> 8)


def native_firefly_step(state) -> None:
    """[asm 0253: 54AB] Step the firefly swarm (animation + both RNGs) in place — the per-frame sim. The VRAM
    draw is the renderer's job; only the state contract (slots + RNG seeds + scratch) is applied here."""
    from pre2.bridge.firefly_sim import read_firefly_sim_state, write_firefly_sim_state
    from pre2.recovered.firefly_sim import step_fireflies
    st = read_firefly_sim_state(state)
    step_fireflies(st)
    write_firefly_sim_state(state, st)


def native_scroll_script(state) -> None:
    """[asm 0256: 3922] Advance the scripted-scroll frame counter [0x2dbe]. A level with an active script (the
    per-level table at [0x2dbc], its first word != -1, on a 4-frame tick) or a nonzero accumulated scroll
    [0x6bf6] (the scripted camera + its VRAM scroll) is unrecovered -> fail loud. A no-script level is a
    byte-exact counter inc."""
    rb, rw = readers(state)
    _ww(state, 0x2DBE, (rw(0x2DBE) + 1) & 0xFFFF)
    if rw(0x6BF6) != 0 or ((rb(0x6BD5) & 3) == 0 and rw(rw(0x2DBC)) != 0xFFFF):
        raise Pre2HybridGap("native scroll-script (3922) active — scripted camera not recovered")


def native_level_state(state) -> None:
    """[asm 0259: 4C69] The per-frame level/death state dispatcher. Idle (mode [0x6be6], respawn [0x6be4], death
    [0x6be5] all 0) it returns no-carry and the loop continues. Armed:
      * [0x6be4]==1 -> the respawn-to-checkpoint handler ``native_4f6c`` (in-loop, no carry; the boss hit set
        [0x6be4]=2 via 8295/65b3 and the player step counts it down 2->1->0 via its timers);
      * [0x6be4]!=0 (i.e. ==2) -> idle this frame (4C69 dispatches nothing while the respawn counter is winding);
      * [0x6be5]==1 death (5063) / ==0xff game-over (5034) / [0x6be6] level-end (4F65) -> the carry paths that
        return to main's level change at 0x12f — not yet recovered, so fail loud (the death/game-over demos)."""
    from pre2.native.level_state import native_4f6c
    rb, _ = readers(state)
    if rb(0x6BE6) != 0:
        raise Pre2HybridGap("native level-state: level-end (4F65 / next-level select, carry -> 0x12f) not recovered")
    if rb(0x6BE4) == 1:
        native_4f6c(state)                                          # [asm 4f6c] respawn-to-checkpoint (in-loop)
        return
    if rb(0x6BE4) != 0:
        return                                                      # [0x6be4]==2: 4C69 idle (counter winding down)
    if rb(0x6BE5) == 1:
        raise Pre2HybridGap("native level-state: death (5063, carry -> 0x12f) not recovered")
    if rb(0x6BE5) == 0xFF:
        raise Pre2HybridGap("native level-state: game-over (5034, carry -> 0x12f) not recovered")


def native_respawn_gate(state) -> None:
    """[asm 0261: 45AF] The respawn-animation pass. Respawning ([0x6be4]!=0) draws the death/respawn sequence
    (from [0x6c0e]/[0x6c10]) — unrecovered. Idle it is the renderer's job (no gameplay state)."""
    rb, _ = readers(state)
    if rb(0x6BE4) != 0:
        raise Pre2HybridGap("native respawn animation (45AF) — not recovered")


def native_special_event(state) -> None:
    """[asm 026A: 67D7] A one-shot event when [0x6ca7] reaches 0x1f (capture player pos -> [0xa336], spawn via
    8d1b). Idle ([0x6ca7] != 0x1f) it is a byte-exact no-op."""
    rb, _ = readers(state)
    if rb(0x6CA7) == 0x1F:
        raise Pre2HybridGap("native special event (67D7: [0x6ca7]==0x1f) not recovered")


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


def native_gameplay_frame(state) -> None:
    """Drive the WHOLE per-frame main loop (0214..0270) over NativeGameState — VM-less, in spine order. The
    recovered gameplay systems run; the render calls (88D7, 8922, and the 3668/35A1/3A27/4B8E/26FA/3721/6772
    cluster) are the faithful renderer's job and are not run here. The event-driven paths a normal frame doesn't
    take — death/respawn + the level-state machine (4C69/45AF), the scripted camera (3922), the 67D7 one-shot —
    are byte-exact no-ops when idle and fail loud when armed (witnessed by the death/game-over demos), never a
    silent skip."""
    rb, rw = readers(state)
    apply_ds(state, tick_popup_ring(rw))                              # [asm 021A] 581E
    native_object_system_step(state)                                 # [asm 0220] 6822 (whole object system)
    apply_ds(state, tick_projectiles(rw, rb))                        # [asm 0223] 6210
    apply_ds(state, tick_particles(rw, rb, tile_reader(state)))      # [asm 0226] 60FE
    apply_ds(state, tick_debris_pool(rw))                            # [asm 0229] 60DF
    _apply_bytes(state, tick_terrain_entities(rw, rb, tile_reader(state)))   # [asm 022C] 4907 (byte-level)
    native_player_step(state)                                       # [asm 022F] 5850 (whole player update)
    native_player_interaction(state)                                # [asm 0232] 8295 (player<->world pass)
    # [asm 0235] 8922 project_particles -> render draw-list (the faithful renderer's job)
    native_trigger_scan(state)                                      # [asm 0238] 52FE (position-trigger; no-op unarmed)
    native_proximity_trigger(state)                                 # [asm 023B] 53F6 (proximity trigger; no-op unfired)
    native_camera_follow(state)                                     # [asm 023E] 5643 (H+V camera follow/scroll)
    # [asm 0241..0250] 3668/35A1/3A27/4B8E/26FA/3721 render cluster — the faithful renderer's job. One gameplay
    # side effect is extracted: 26FA (object_render) bumps the free-running 16-bit frame counter [0x6bd5] that
    # the animation phase reads ([0x6bd5]&1/&3/&7/&0xf) — in 11 gameplay calls incl. the player/object/particle
    # passes. The prefix above already read it as N (frame start); the firefly/scroll/respawn/shake below read N+1.
    _ww(state, 0x6BD5, (rw(0x6BD5) + 1) & 0xFFFF)                    # [asm 024D: 2708] inc word [0x6bd5]
    native_firefly_step(state)                                      # [asm 0253] 54AB
    native_scroll_script(state)                                     # [asm 0256] 3922
    native_level_state(state)                                       # [asm 0259] 4C69 (carry -> level change @ 0x12f)
    native_respawn_gate(state)                                      # [asm 0261] 45AF
    # [asm 0264] 44FB (4509 + 1C65) render/timing helper; [asm 0267] 6772 render commit — the renderer's job
    native_special_event(state)                                     # [asm 026A] 67D7
    native_camera_shake(state)                                      # [asm 026D] 4C30
    # [asm 0270] jmp 0214 — loop back


def native_death_bounce_509d(state) -> None:
    """[asm 509D] The death-bounce animation — the player's death-jump (called first by the respawn 4F6C and the
    death 5063). Sets the player flying (Yvel=+0xF, anim 0x21) drifting toward the screen centre (Xvel=+/-5), then
    runs 0x3C=60 frames of the entity-update SUBSET (581E/6822/6210/60FE/60DF + the 26FA frame-counter tick +
    54AB/3922) while integrating the player ballistically with gravity (NO collision — the corpse arcs up then
    falls through the level). The render cluster (3668..3721, 44FB) is the renderer's job. Blocking: it plays out
    60 whole frames in one call, reusing the same recovered leaves as native_gameplay_frame.

    Verified: the player's death-bounce TRAJECTORY is byte-exact vs the ASM (timer-driven synthetic invoke — the
    render busy-waits need the full timing machinery, so it's driven through play._advance_demo_frame). The only
    residual is the 8 effect slots that the render 26FA frees ([slot+4]=0xffff) as their lifetime expires —
    render-managed, and in the full 4F6C respawn it is immediately wiped by 5237's pool re-init, so it is
    irrelevant to the respawn outcome (and would be reproduced by native_render's per-frame draw in a renderer)."""
    def _s16(v):
        return v - 0x10000 if v & 0x8000 else v

    rb, rw = readers(state)
    # [asm 50a6-50b7] play_sfx(7) (an audio command, no DGROUP) + the death pose
    _wb(state, 0x4F2D, 0)                                            # [asm 50ac] clear the player death-state byte
    _ww(state, 0x4F20, 0x21)                                        # [asm 50b1] death anim frame
    _ww(state, 0x4F2A, 0x0F)                                        # [asm 50b7] Yvel = +15 (the upward kick)
    centre = ((rw(0x2DE4) + 0xA) << 4) & 0xFFFF                     # [asm 50c0-50cd] (camera cell + 10 tiles) * 16
    _ww(state, 0x4F22, 5 if rw(0x4F1C) < centre else (-5 & 0xFFFF))  # [asm 50cf-50d8] drift toward screen centre
    for _ in range(0x3C):                                           # [asm 50db cx=0x3c] 60 frames
        _ww(state, 0x4F0E, 0xFFFF)                                  # [asm 50df] suppress the normal player render
        apply_ds(state, tick_popup_ring(rw))                        # [asm 50e5] 581E
        native_object_system_step(state)                           # [asm 50e8] 6822
        apply_ds(state, tick_projectiles(rw, rb))                  # [asm 50eb] 6210
        apply_ds(state, tick_particles(rw, rb, tile_reader(state)))  # [asm 50ee] 60FE
        apply_ds(state, tick_debris_pool(rw))                      # [asm 50f1] 60DF
        # [asm 50f4-5103] 3668/35A1/3A27/4B8E/26FA/3721 render — only 26FA's [0x6bd5] frame tick is gameplay:
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
