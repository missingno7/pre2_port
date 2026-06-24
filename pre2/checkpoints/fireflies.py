"""Native replacement for the firefly swarm (1030:54AB) — the perf win: Python owns the whole pass.

`pre2.recovered.fireflies.draw_fireflies` already lets the faithful renderer SHOW the swarm, but the
per-frame animation still ran as interpreted ASM. This checkpoint replaces 54AB outright: the recovered
:func:`pre2.recovered.firefly_sim.step_fireflies` advances all 20 slots (RNG-driven flocking) and draws
them, so the VM skips the routine entirely.

The contract is unusually wide because 54AB drives two SHARED RNGs (26CF/39DF) that the rest of the game
also draws from — the replacement MUST reproduce them byte-exact or the whole game desyncs. Proven by
pre2/probes/verify_firefly_sim.py (40 frames, 0 mismatches: slots, both RNG seeds, the [0x6BC0]/[0x6BC1]
scratch, and the drawn VRAM). Verify mode keeps the ASM as oracle and diffs that state contract at the ret.

After the pass the ASM leaves the EGA registers at map_mask=0x0F, logical_op=OR, data_rotate=0,
write_mode=0 (from the 452B reset + the swarm's draw setup); the replacement sets the same so later draws
see identical EGA state.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.firefly_sim import read_firefly_sim_state, write_firefly_sim_state
from pre2.recovered.firefly_sim import render_step_into, step_fireflies

from .common import report

_ENTRY = (0x1030, 0x54AB)
_EXIT = (0x1030, 0x55FB)
_DATA = 0x1A0F


def _draw_into_vram(mem, st) -> None:
    mv = memoryview(mem.data)
    planes = [mv[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000]
              for p in range(4)]
    render_step_into(st, planes)


def _set_exit_ega_state(mem) -> None:
    """The EGA register state the ASM pass leaves (452B reset + 54B1/54B8 draw setup)."""
    mem.ega_map_mask = 0x0F
    mem.ega_logical_op = 2          # GC function select = OR
    mem.ega_data_rotate = 0
    mem.ega_write_mode = 0


@registry.replace(*_ENTRY, "firefly_sim")
def firefly_sim(cpu) -> None:
    """Mode-2 replacement at 1030:54AB (CALL'd routine -> ret 55FB).

    Live hybrid: step the swarm (animation + RNG), write the contract (slots, both RNG seeds, scratch),
    draw into VRAM, restore the EGA exit state, and return — skipping the ASM body. Verify mode shadows
    the prediction at entry and passes through so the ASM stays the oracle."""
    if getattr(cpu, "pre2_verify_mode", False):
        st = read_firefly_sim_state(cpu.mem)
        step_fireflies(st)
        cpu.pre2_firefly_pending.append(
            (bytes(st.slots), st.rng_a, tuple(st.rng_b), tuple(st.scratch)))
        interpret_current_instruction_without_hook(cpu)
        return
    mem = cpu.mem
    st = read_firefly_sim_state(mem)
    step_fireflies(st)
    write_firefly_sim_state(mem, st)
    _draw_into_vram(mem, st)
    _set_exit_ega_state(mem)
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the swarm's ret (55FB)."""

    def _rb(off):
        return cpu.mem.data[((_DATA << 4) + off) & 0xFFFFF]

    def _r16(off):
        a = ((_DATA << 4) + off) & 0xFFFFF
        return cpu.mem.data[a] | (cpu.mem.data[a + 1] << 8)

    def _verify_at_exit(c) -> None:
        if c.pre2_firefly_pending:
            slots, rng_a, rng_b, scratch = c.pre2_firefly_pending.pop()
            sbase = ((_DATA << 4) + 0x6EA9) & 0xFFFFF
            a_slots = bytes(c.mem.data[sbase:sbase + 160])
            a_a = _r16(0x28C1)
            a_b = (_r16(0x2CEF), _rb(0x2CEC), _rb(0x2CED), _rb(0x2CEE))
            a_scr = (_rb(0x6BC0), _rb(0x6BC1))
            reason = None
            if a_slots != slots:
                i = next(k for k in range(160) if a_slots[k] != slots[k])
                reason = f"slots[{i}] (slot {i // 8}+{i % 8}): asm={a_slots[i]:#04x} rec={slots[i]:#04x}"
            elif a_a != rng_a:
                reason = f"rng_a[28C1]: asm={a_a:#06x} rec={rng_a:#06x}"
            elif a_b != rng_b:
                reason = f"rng_b: asm={a_b} rec={rng_b}"
            elif a_scr != scratch:
                reason = f"scratch[6BC0/1]: asm={a_scr} rec={scratch}"
            report(stats, on_result, raise_on_divergence, "firefly_sim", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "firefly_sim_verify"
