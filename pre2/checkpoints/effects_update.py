"""Checkpoint for the secondary-entity update-pass leaves — see :mod:`pre2.recovered.effects_update`.

Each leaf is CALL'd from the main-loop subsystem spine (1030:021A..0229) with scratch registers (the spine
re-derives them), so the live hook runs the recovered tick over VM memory, applies the DGROUP write contract,
and does the near ret. Like object_tick / player_interaction this collapses the pass into one host step (NOT
instruction-count transparent); the data-segment effect is byte-exact (shadow-proven over the gorilla/bonus
demos, 0 divergences).

Verify mode keeps the ASM as oracle: predict the write contract from the entry state (no mutation), step
aside, and diff the contract at the routine's RET.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import effects_update as bridge
from pre2.recovered.effects_update import tick_debris_pool, tick_particles, tick_popup_ring

from .common import report

_DS_BASE = (0x1A0F << 4) & 0xFFFFF

# name -> (entry, ret)
_LEAVES = {
    "tick_popup_ring": (0x581E, 0x584F),
    "tick_particles": (0x60FE, 0x620F),
    "tick_debris_pool": (0x60DF, 0x60FD),
}


def _writes(mem, name):
    """Recovered write contract for one leaf over live DGROUP memory."""
    rb, rw = bridge.readers(mem)
    if name == "tick_particles":
        return tick_particles(rw, rb, bridge.tile_reader(mem))
    if name == "tick_popup_ring":
        return tick_popup_ring(rw)
    return tick_debris_pool(rw)


def _run(cpu, ret, name):
    mem = cpu.mem
    writes = _writes(mem, name)
    if getattr(cpu, "pre2_verify_mode", False):
        pend = getattr(cpu, "pre2_eu_pending", None)
        if pend is None:
            pend = cpu.pre2_eu_pending = {}
        pend[ret] = writes
        interpret_current_instruction_without_hook(cpu)
        return
    bridge.apply_ds(mem, writes)
    cpu.s.ip = cpu.pop()       # near ret to the main-loop spine; regs are scratch


@registry.replace(0x1030, 0x581E, "tick_popup_ring")
def _popup_ring_hook(cpu) -> None:
    _run(cpu, 0x584F, "tick_popup_ring")


@registry.replace(0x1030, 0x60FE, "tick_particles")
def _particles_hook(cpu) -> None:
    _run(cpu, 0x620F, "tick_particles")


@registry.replace(0x1030, 0x60DF, "tick_debris_pool")
def _debris_pool_hook(cpu) -> None:
    _run(cpu, 0x60FD, "tick_debris_pool")


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Diff each leaf's predicted DGROUP write contract vs the ASM at its RET (live --verify-hooks coverage)."""
    if not hasattr(cpu, "pre2_eu_pending"):
        cpu.pre2_eu_pending = {}

    def _make_diff(ret, name):
        def _diff(c) -> None:
            writes = getattr(c, "pre2_eu_pending", {}).pop(ret, None)
            if writes:
                reason = None
                for off, (val, wid) in writes.items():
                    for k in range(wid):
                        asm = c.mem.data[(_DS_BASE + ((off + k) & 0xFFFF)) & 0xFFFFF]
                        if asm != ((val >> (8 * k)) & 0xFF):
                            reason = f"{name} [{(off + k):#06x}] rec={(val >> (8 * k)) & 0xFF:#04x} asm={asm:#04x}"
                            break
                    if reason:
                        break
                report(stats, on_result, raise_on_divergence, name, reason)
            interpret_current_instruction_without_hook(c)
        return _diff

    for name, (entry, ret) in _LEAVES.items():
        cpu.replacement_hooks[(0x1030, ret)] = _make_diff(ret, name)
        cpu.hook_names[(0x1030, ret)] = name + "_verify"
