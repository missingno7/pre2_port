"""Checkpoint for the effect-sprite projector (1030:8922..899D).

Called once per frame from the main loop (~1030:0235). It animates + projects the on-screen entries of the
fixed 70-entry free-floating effect-sprite list at DS:0x8F1D into the render-slot array at DS:0x52E8 (see
:func:`pre2.recovered.object_particles.project_particles`). These are small decorative/effect sprites that float
with a per-entry ping-pong "bounce" (e.g. collectible sparkles, weather like the penguin-level snow) — NOT the
player throwing-weapon projectiles (those live in a separate list at 0x4F2E, drawn by 1030:88D7).

Recovered byte-exact in shadow (456 calls, 0 mismatches across six demos; projection + bounce paths exercised).
Thin VM contact point: read DS state, run :func:`project_particles` for the ``{offset: (value, width)}`` write
contract, then apply it + emulate the RET (live), or predict-and-diff at the 899D ret (verify).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.object_particles import project_particles

from .common import report

_ENTRY = (0x1030, 0x8922)
_EXIT = (0x1030, 0x899D)   # the ret
_DS = 0x1A0F


def _readers(mem):
    ds_base = (_DS << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(ds_base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        b = (ds_base + (o & 0xFFFF)) & 0xFFFFF
        return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)

    return rb, rw, ds_base


def _apply(mem, ds_base, writes):
    for off, (val, width) in writes.items():
        b = (ds_base + (off & 0xFFFF)) & 0xFFFFF
        mem.data[b] = val & 0xFF
        if width == 2:
            mem.data[(b + 1) & 0xFFFFF] = (val >> 8) & 0xFF


@registry.replace(*_ENTRY, "object_particles")
def object_particles_hook(cpu) -> None:
    """Native replacement for the effect-sprite projector at 1030:8922."""
    mem = cpu.mem
    rb, rw, ds_base = _readers(mem)
    writes = project_particles(rb, rw)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_particles_pending.append(writes)
        interpret_current_instruction_without_hook(cpu)
        return

    _apply(mem, ds_base, writes)
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at the routine return (899D): diff every predicted DS byte/word
    (computed at entry from the pre-state) against the ASM's post-state."""
    cpu.pre2_particles_pending = []

    def _verify_at_exit(c) -> None:
        pending = getattr(c, "pre2_particles_pending", None)
        if pending:
            writes = pending.pop()
            ds_base = (_DS << 4) & 0xFFFFF
            reason = None
            for off, (val, width) in writes.items():
                b = (ds_base + (off & 0xFFFF)) & 0xFFFFF
                act = c.mem.data[b]
                if width == 2:
                    act |= c.mem.data[(b + 1) & 0xFFFFF] << 8
                if act != (val & (0xFF if width == 1 else 0xFFFF)):
                    reason = f"ds[{off:#06x}] rec={val:#06x} asm={act:#06x}"
                    break
            report(stats, on_result, raise_on_divergence, "object_particles", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "object_particles_verify"
