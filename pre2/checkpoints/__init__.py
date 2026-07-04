"""Native replacement checkpoints — the hybrid runtime for Prehistorik 2.

Each recovered subsystem is installed as a thin adapter at the original routine's
CS:IP via the shared ``registry`` (one module per subsystem in this package). In
normal play these run **instead of** the original ASM (the hybrid runtime gets
faster as coverage grows); under verification they run as a parallel oracle check.

General mechanism (kept deliberately small to avoid per-hook swell):
- a pure, VM-independent recovered function (e.g. ``pre2.codecs.sqz.unpack_sqz``);
- a thin adapter that reads original VM state, calls the pure function, writes the
  *contract* back (the game-visible outputs), and returns to original flow;
- one verification path that diffs that same contract against the original ASM.

These checkpoints are **scaffolding, not architecture**: as islands merge into
recovered subsystems the contact points should rise (byte/buffer diffs → semantic
state contracts) and grow fewer. See docs/pre2/source_port_plan.md.

Install with :func:`install_pre2_replacements` (hybrid, default) and optionally
:func:`enable_pre2_hook_verification` (the lockstep oracle, opt-in).
"""

from __future__ import annotations

from dos_re.hooks import registry

from . import animation, audio, blit, camera_shake, effects_update, fireflies, foreground_tiles, frame, gameover_scroll, hud, input_decode, object_inject, object_interaction, object_particles, object_render, object_spawn, object_tick, object_update, oldies_text, palette, particles, player, player_collision, player_interaction, present, sprite_classify, sprite_decode, sqz, tally_panel, terrain_entities, text, tracker, transition  # noqa: F401 — import to register @registry.replace hooks
from pre2.gaps import HookTraceStats, HookVerifyStats, Pre2HybridGap  # noqa: F401 — re-exported
from .sprite_decode import sprite_decode_local, sprite_decode_shared  # noqa: F401 — re-exported
from .sqz import sqz_decompress  # noqa: F401 — re-exported

__all__ = [
    "install_pre2_replacements",
    "uninstall_pre2_replacements",
    "enable_pre2_hook_verification",
    "enable_pre2_hook_trace",
    "HookVerifyStats",
    "HookTraceStats",
    "Pre2HybridGap",
]


# The SAFE-ORACLE hook subset: hooks whose write-set is entirely RENDER/AUDIO-owned state (VRAM planes, the
# DAC, and DGROUP offsets the gameplay digest already EXCLUDES — the same ownership boundary pre2/native/
# seams.py draws). Property: a bug in one of these CANNOT corrupt the gameplay state a recorded demo
# certifies — the game LOGIC trajectory is produced by original ASM only. That makes `--safe-hooks` a
# fluent-yet-trustworthy ORACLE mode for recording demos: the render/mixer instruction sinks collapse
# (playable wall-clock) while every gameplay-owned byte still comes from PRE2.EXE.
#   Deliberately EXCLUDED (each writes gameplay-owned state the game recomputes per frame): object_render
#   (26FA [0x6BD5]++, gameplay-gated at 51F0/5427), particles_draw (4B8E advances+kills the [0x7DE6] particle
#   list), bg_anim_advance (anim phase tables), firefly_sim (consumes the gameplay rng [0x2CEC]),
#   camera_shake_apply ([0x6BF8]), and every logic hook (player_*, object_*, input_decode, second_pass_*,
#   terrain, interaction, bosses).
_RENDER_AUDIO_OWNED = frozenset({
    "frame_tile_row", "frame_grid", "frame_scroll_copy", "frame_panel_copy",   # frame renderer -> VRAM
    "sprite_blit",                                                             # 3B88 blit leaf -> VRAM + di
    "foreground_tiles",                                                        # z-order redraw -> VRAM
    "palette_fade",                                                            # DAC only
    "draw_string", "oldies_glyph", "gameover_scroll", "tally_panel",           # scene/text draws -> VRAM
    "iris_transition",                                                         # VRAM + iris scratch (excluded)
    "scroll_blit", "scroll_shift",                                             # menu/present scroll -> VRAM
    "audio_mix_channel", "audio_tracker_tick",                                 # mixer/tracker (audio-owned)
})
# The ASSET-DECODE tier: justified by a DIFFERENT argument than write-set ownership. Their output (level
# maps, sprite banks) IS gameplay-read, so a bug here COULD corrupt the trajectory — but their input domain
# is CLOSED: a finite set of .SQZ files, and the decode is a pure function of the file. Recovered==ASM is
# proven over the ENTIRE real domain, offline: pre2/probes/verify_sqz_all_assets.py sweeps every asset
# through the original 107B and memcmps against unpack_sqz (37/40 byte-identical; the 3 non-matches are
# outside the hook's domain or probe-context artifacts with independent real-context proof: PRE2.SQZ is the
# packed game EXE the bootstrap decodes, never 107B; SPRITES.SQZ hit the loader's out-of-memory halt `jmp $`
# in the synthetic mid-game context — its real boot-context load is byte-verified by the SHARED-bank 83/83
# witness; PRESENT.SQZ's real boot decode is proven by the pixel-exact recovered title screens). The sprite
# demux pair carries the LOCAL 173/173 + SHARED 83/83 witnesses plus the finish-game demo native==pure-ASM
# equality across every level load. Collapsing these removes the multi-second interpreted level-load stalls.
_ASSET_DECODE_INPUT_CLOSED = frozenset({
    "sqz_decompress",                                                          # 107B loader+codec (all formats)
    "sprite_decode_local", "sprite_decode_shared",                             # planar sprite/tile demux
})
# The OBJECT-RENDER tier: 26FA, the moving-sprite draw pass. The wall-clock decider — with the render/audio
# tier live and the waits collapsed, its interpreted body (the per-sprite planar blit loops, 2700..2DFF) is
# ~75% of ALL remaining interpreted instructions in gameplay (profiled on snapshot_pre2_20260623_192040).
# Its gameplay-visible writes are narrow and individually proven: the [0x6BD5] frame counter (a single
# lockstep-verified `inc word`; gameplay gates 51F0/5427 read it) and the digest-EXCLUDED render-record pool
# (slot life/flag mutation — allocation-order effects proven non-cascading into gameplay on the full gorilla
# + finish-game demos, see _PROJ_PTR in pre2/native/seams.py). Verify-enabled (object_render register_verify)
# so any drift is diffable against the ASM at its RET.
_OBJECT_RENDER_VERIFIED = frozenset({"object_render"})
SAFE_ORACLE_HOOKS = _RENDER_AUDIO_OWNED | _ASSET_DECODE_INPUT_CLOSED | _OBJECT_RENDER_VERIFIED


def install_pre2_replacements(rt, *, mode: str = "hybrid") -> int:
    """Install the native replacement hooks. Returns the installed count.

    ``mode="hybrid"`` (default): every ``@registry.replace`` hook — the full hybrid runtime.
    ``mode="safe"``: only :data:`SAFE_ORACLE_HOOKS` — original ASM game logic with the render/audio
    instruction sinks collapsed (the fluent demo-recording oracle). A safe-set name missing from the
    registry fails loud so the list cannot drift.

    Note ``dos_re.create_runtime`` already auto-installs every ``@registry.replace``
    hook; this additionally wires the asset resolver the hooks need.
    """
    rt.cpu.pre2_dos = rt.dos
    registry.install(rt.cpu)
    if mode == "safe":
        known = {repl.name for repl in registry.replacements.values()}
        missing = SAFE_ORACLE_HOOKS - known
        if missing:
            raise ValueError(f"SAFE_ORACLE_HOOKS drift — not in the registry: {sorted(missing)}")
        n = 0
        for key, repl in registry.replacements.items():
            if repl.name in SAFE_ORACLE_HOOKS:
                n += 1
            else:                                       # gameplay-owned hook: fall back to original ASM
                rt.cpu.replacement_hooks.pop(key, None)
                rt.cpu.hook_names.pop(key, None)
        return n
    if mode != "hybrid":
        raise ValueError(f"unknown replacement mode {mode!r} (hybrid|safe)")
    return len(registry.replacements)


def uninstall_pre2_replacements(rt) -> None:
    """Remove the native replacement hooks so the runtime executes pure original
    ASM — used for capturing reference output and as the verification oracle."""
    for key in registry.replacements:
        rt.cpu.replacement_hooks.pop(key, None)
        rt.cpu.hook_names.pop(key, None)


def enable_pre2_hook_verification(rt, *, on_result=None, raise_on_divergence=False):
    """Run replacement hooks as a parallel oracle check instead of replacing.

    Flips the hooks into verify mode: the original ASM executes (the oracle) and
    each native result is diffed against it at the routine's return boundary, over
    the game-visible *contract* only. Each subsystem installs its own verify-exit
    hooks via ``register_verify``. Returns live-updating :class:`HookVerifyStats`.
    Meant for offline replay of demos/snapshots.
    """
    cpu = rt.cpu
    cpu.pre2_dos = rt.dos
    registry.install(cpu)
    cpu.pre2_verify_mode = True
    cpu.pre2_verify_pending = []
    cpu.pre2_sprite_pending = []
    cpu.pre2_blit_pending = []
    cpu.pre2_frame_pending = []
    cpu.pre2_frame_grid_pending = []
    cpu.pre2_frame_scroll_pending = []
    cpu.pre2_frame_panel_pending = []
    cpu.pre2_audio_pending = []
    cpu.pre2_tracker_pending = []
    cpu.pre2_object_pending = []
    cpu.pre2_classify_pending = []
    cpu.pre2_palette_pending = []
    cpu.pre2_anim_pending = []
    cpu.pre2_shake_pending = []
    cpu.pre2_firefly_pending = []
    cpu.pre2_gameover_scroll_pending = []
    cpu.pre2_tally_panel_pending = []
    cpu.pre2_iris_pending = []
    cpu.pre2_text_pending = []
    cpu.pre2_scroll_pending = []
    cpu.pre2_scroll_shift_pending = []
    cpu.pre2_particles_pending = []
    cpu.pre2_foreground_pending = []
    cpu.pre2_inject_pending = []
    cpu.pre2_player_pending = []
    cpu.pre2_player_y_pending = []
    cpu.pre2_player_t_pending = []
    cpu.pre2_fsm_pending = []
    cpu.pre2_collision_pending = []
    stats = HookVerifyStats()
    sqz.register_verify(cpu, stats, on_result, raise_on_divergence)
    sprite_decode.register_verify(cpu, stats, on_result, raise_on_divergence)
    blit.register_verify(cpu, stats, on_result, raise_on_divergence)
    frame.register_verify(cpu, stats, on_result, raise_on_divergence)
    audio.register_verify(cpu, stats, on_result, raise_on_divergence)
    tracker.register_verify(cpu, stats, on_result, raise_on_divergence)
    object_render.register_verify(cpu, stats, on_result, raise_on_divergence)
    object_update.register_verify(cpu, stats, on_result, raise_on_divergence)
    object_inject.register_verify(cpu, stats, on_result, raise_on_divergence)
    sprite_classify.register_verify(cpu, stats, on_result, raise_on_divergence)
    palette.register_verify(cpu, stats, on_result, raise_on_divergence)
    animation.register_verify(cpu, stats, on_result, raise_on_divergence)
    camera_shake.register_verify(cpu, stats, on_result, raise_on_divergence)
    fireflies.register_verify(cpu, stats, on_result, raise_on_divergence)
    gameover_scroll.register_verify(cpu, stats, on_result, raise_on_divergence)
    tally_panel.register_verify(cpu, stats, on_result, raise_on_divergence)
    hud.register_verify(cpu, stats, on_result, raise_on_divergence)
    transition.register_verify(cpu, stats, on_result, raise_on_divergence)
    text.register_verify(cpu, stats, on_result, raise_on_divergence)
    present.register_verify(cpu, stats, on_result, raise_on_divergence)
    particles.register_verify(cpu, stats, on_result, raise_on_divergence)
    foreground_tiles.register_verify(cpu, stats, on_result, raise_on_divergence)
    player.register_verify(cpu, stats, on_result, raise_on_divergence)
    player_collision.register_verify(cpu, stats, on_result, raise_on_divergence)
    player_interaction.register_verify(cpu, stats, on_result, raise_on_divergence)
    object_particles.register_verify(cpu, stats, on_result, raise_on_divergence)
    object_interaction.register_verify(cpu, stats, on_result, raise_on_divergence)
    effects_update.register_verify(cpu, stats, on_result, raise_on_divergence)
    terrain_entities.register_verify(cpu, stats, on_result, raise_on_divergence)
    return stats


def enable_pre2_hook_trace(rt) -> HookTraceStats:
    """Run the **live hybrid runtime** (replacement hooks running instead of the ASM) and
    additionally count each hook's invocations by name — so you can watch which recovered
    systems are actually live, and see where the game is still pure ASM (the hooks simply
    never fire there). Unlike :func:`enable_pre2_hook_verification` there is no oracle and
    no diff: the real hooks run, each wrapped in a tally. Returns a live :class:`HookTraceStats`.
    """
    cpu = rt.cpu
    cpu.pre2_dos = rt.dos
    registry.install(cpu)
    stats = HookTraceStats()
    for key in list(cpu.replacement_hooks):
        fn = cpu.replacement_hooks[key]
        name = cpu.hook_names.get(key) or "%04x:%04x" % key

        def make(fn, name):
            def wrapped(c):
                stats.bump(name)
                return fn(c)
            return wrapped

        cpu.replacement_hooks[key] = make(fn, name)
    return stats
