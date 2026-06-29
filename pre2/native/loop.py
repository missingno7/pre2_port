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
    (0x5850, "gap", "unclassified"),
    (0x8295, "native", "player_interaction"),
    (0x8922, "render", "project_particles (effect-sprite projector -> render slots)"),
    (0x52FE, "gap", "unclassified"),
    (0x53F6, "gap", "unclassified"),
    (0x5643, "gap", "unclassified"),
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


def native_gameplay_frame(state) -> None:
    """Drive the recovered prefix of the per-frame main loop (0214..) over NativeGameState — VM-less, in spine
    order, fail-loud at the first gap. Today reaches 0x5850 (the first unclassified gap); the prefix grows as
    gaps are recovered. Render calls (88D7) are the faithful renderer's job and are not run here."""
    rb, rw = readers(state)
    apply_ds(state, tick_popup_ring(rw))                              # [asm 021A] 581E
    native_object_system_step(state)                                 # [asm 0220] 6822 (whole object system)
    apply_ds(state, tick_projectiles(rw, rb))                        # [asm 0223] 6210
    apply_ds(state, tick_particles(rw, rb, tile_reader(state)))      # [asm 0226] 60FE
    apply_ds(state, tick_debris_pool(rw))                            # [asm 0229] 60DF
    _apply_bytes(state, tick_terrain_entities(rw, rb, tile_reader(state)))   # [asm 022C] 4907 (byte-level)
    raise Pre2HybridGap("main-loop 0x5850 (first unclassified gap) not yet recovered")   # [asm 022F]
