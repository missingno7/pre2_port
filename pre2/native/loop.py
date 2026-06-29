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
from pre2.checkpoints.common import Pre2HybridGap
from pre2.recovered.object_spawn import Pre2SpawnGap, camera_engine, tick_mode9_boss

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
