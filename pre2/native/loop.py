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
    (0x3668, "gap", "unclassified"),
    (0x35A1, "gap", "unclassified"),
    (0x3A27, "gap", "unclassified"),
    (0x4B8E, "render", "particles_draw"),
    (0x26FA, "gap", "unclassified"),
    (0x3721, "gap", "trigger system"),
    (0x54AB, "native", "firefly_sim"),
    (0x3922, "gap", "scroll script"),
    (0x4C69, "gap", "unclassified"),
    (0x45AF, "gap", "unclassified"),
    (0x44FB, "gap", "unclassified"),
    (0x6772, "render", "render-frame commit (-> faithful renderer)"),
    (0x67D7, "gap", "unclassified"),
    (0x4C30, "gap", "unclassified"),
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


def native_gameplay_frame(state) -> None:
    """Drive the recovered prefix of the per-frame main loop (0214..) over NativeGameState — VM-less, in spine
    order, fail-loud at the first gap. Today reaches the 0x3668 render cluster (after the player,
    player-interaction, the two trigger scans, and the camera follow); the prefix grows as gaps are recovered.
    Render calls (88D7, 8922, and the 3668.. cluster) are the faithful renderer's job and are not run here."""
    rb, rw = readers(state)
    apply_ds(state, tick_popup_ring(rw))                              # [asm 021A] 581E
    native_object_system_step(state)                                 # [asm 0220] 6822 (whole object system)
    apply_ds(state, tick_projectiles(rw, rb))                        # [asm 0223] 6210
    apply_ds(state, tick_particles(rw, rb, tile_reader(state)))      # [asm 0226] 60FE
    apply_ds(state, tick_debris_pool(rw))                            # [asm 0229] 60DF
    _apply_bytes(state, tick_terrain_entities(rw, rb, tile_reader(state)))   # [asm 022C] 4907 (byte-level)
    native_player_step(state)                                       # [asm 022F] 5850 (whole player update)
    native_player_interaction(state)                                # [asm 0232] 8295 (player<->world pass)
    # [asm 0235] 8922 project_particles -> render draw-list (the faithful renderer's job, not run here)
    native_trigger_scan(state)                                      # [asm 0238] 52FE (position-trigger; no-op unarmed)
    native_proximity_trigger(state)                                 # [asm 023B] 53F6 (proximity trigger; no-op unfired)
    native_camera_follow(state)                                     # [asm 023E] 5643 (H+V camera follow/scroll)
    # [asm 0241..] 3668/35A1/3A27/4B8E/26FA render cluster (frame_renderer/object_render) — the renderer's job
    raise Pre2HybridGap("main-loop 0x3668 (render cluster after the camera follow) not run in the gameplay step")  # [asm 0241]
