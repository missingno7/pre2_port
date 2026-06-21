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

from . import audio, blit, frame, object_render, sprite_decode, sqz, tracker  # noqa: F401 — import to register @registry.replace hooks
from .blit import _BLIT_ENTRY, _BLIT_EXITS  # noqa: F401 — re-exported for probes
from .common import HookVerifyStats, Pre2HybridGap  # noqa: F401 — re-exported
from .sprite_decode import sprite_decode_local, sprite_decode_shared  # noqa: F401 — re-exported
from .sqz import sqz_decompress  # noqa: F401 — re-exported

__all__ = [
    "install_pre2_replacements",
    "uninstall_pre2_replacements",
    "enable_pre2_hook_verification",
    "HookVerifyStats",
    "Pre2HybridGap",
]


def install_pre2_replacements(rt) -> int:
    """Install the native replacement hooks (the hybrid runtime). Returns count.

    Note ``dos_re.create_runtime`` already auto-installs every ``@registry.replace``
    hook; this additionally wires the asset resolver the hooks need.
    """
    rt.cpu.pre2_dos = rt.dos
    registry.install(rt.cpu)
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
    stats = HookVerifyStats()
    sqz.register_verify(cpu, stats, on_result, raise_on_divergence)
    sprite_decode.register_verify(cpu, stats, on_result, raise_on_divergence)
    blit.register_verify(cpu, stats, on_result, raise_on_divergence)
    frame.register_verify(cpu, stats, on_result, raise_on_divergence)
    audio.register_verify(cpu, stats, on_result, raise_on_divergence)
    tracker.register_verify(cpu, stats, on_result, raise_on_divergence)
    object_render.register_verify(cpu, stats, on_result, raise_on_divergence)
    return stats
