"""Native replacement for the point-particle pass (1030:4B8E) — the per-frame advance + draw + kill.

``draw_particles`` already let the faithful renderer re-draw the particles from a snapshot; this grounds
the FULL 4B8E pass back into the live hybrid runtime as a real ASM replacement (not a faithful shadow).
The pass: ``452b`` (GC reset to write-mode 0 / copy) -> EGA setup (seq map-mask 0x0F, GC function = OR) ->
for each active slot advance X/Y, plot one white pixel (OR into all 4 planes) at page ``[0x2DD8]`` if
on-screen, write the advanced Y back to ``[slot+2]`` and kill the slot (``[slot]=0xFFFF``) -> ret (4C2F).

The replacement runs ``consume_particles`` (draw + per-slot writeback) on the live VRAM planes, applies
the array writeback, and replicates the EGA register OUTs so the VM's EGA state matches the ASM exactly
(the recovered draw bypasses the EGA write path, so the registers must be set explicitly for later draws).
The ``push es``/``pop es`` net-cancel, so es is unchanged; the routine is CALL'd, so the exit pops the
near return address. Verify mode shadows the predicted planes + array at entry and diffs at the ret (4C2F).
Proven byte-exact (whole-state) by pre2/probes/verify_particles_hook.py.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge.particles import (PARTICLE_BASE, apply_particle_writeback,
                                   read_particle_consume_inputs)
from pre2.bridge.sprites import plane_views
from pre2.recovered.particles import PARTICLE_COUNT, PARTICLE_STRIDE, consume_particles

from .common import report

_ENTRY = (0x1030, 0x4B8E)
_EXIT = (0x1030, 0x4C2F)
_DATA = 0x1A0F
_ARRAY_LEN = PARTICLE_COUNT * PARTICLE_STRIDE


def _set_ega_state(cpu) -> None:
    """Replicate the EGA register OUTs 4B8E performs (452b GC reset + the OR-plot setup + exit) so the
    VM's EGA state after the replacement is identical to the ASM's."""
    pw = cpu.port_writer
    pw(cpu, 0x3CE, 0x0005, 16)   # [452b 4530] GC reg5 = 0 -> write mode 0
    pw(cpu, 0x3CE, 0x0003, 16)   # [452b 4534] GC reg3 = 0 -> copy
    pw(cpu, 0x3C4, 0x0F02, 16)   # [4B95] seq reg2 = 0x0F -> map mask all planes
    pw(cpu, 0x3CE, 0x1003, 16)   # [4B9B] GC reg3 = 0x10 -> function OR
    pw(cpu, 0x3CE, 0x0001, 16)   # [4B9F] GC reg1 = 0 -> enable-set/reset off
    pw(cpu, 0x3CE, 0x0001, 16)   # [4C2A] GC reg1 = 0 (exit)


def _array(mem):
    b = (_DATA << 4) + PARTICLE_BASE
    return mem.data[b:b + _ARRAY_LEN]


@registry.replace(*_ENTRY, "particles_draw")
def particles_draw(cpu) -> None:
    """Mode-2 replacement at 1030:4B8E (CALL'd -> ret 4C2F): advance + draw + writeback + kill, live."""
    mem = cpu.mem
    slots, cam_col, cam_row, y_bias, page, cos, sin = read_particle_consume_inputs(mem)
    if getattr(cpu, "pre2_verify_mode", False):
        rec = [bytearray(bytes(pl)) for pl in plane_views(mem)]
        wb = consume_particles(rec, slots, cam_col, cam_row, y_bias, page, cos, sin)
        arr = bytearray(_array(mem))
        for index, ny in wb:                       # predict the array writeback on a copy
            o = index * PARTICLE_STRIDE
            arr[o + 2] = ny & 0xFF
            arr[o + 3] = (ny >> 8) & 0xFF
            arr[o] = 0xFF
            arr[o + 1] = 0xFF
        cpu.pre2_particles_pending.append((rec, bytes(arr)))
        interpret_current_instruction_without_hook(cpu)
        return
    _set_ega_state(cpu)
    planes = plane_views(mem)
    wb = consume_particles(planes, slots, cam_col, cam_row, y_bias, page, cos, sin)
    apply_particle_writeback(mem, wb)
    cpu.s.ip = cpu.pop()                           # near ret (push es/pop es net-cancel)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the pass ret (4C2F): diff predicted planes + array."""

    def _verify_exit(c) -> None:
        if getattr(c, "pre2_particles_pending", None):
            rec, arr = c.pre2_particles_pending.pop()
            planes = plane_views(c.mem)
            reason = None
            for p in range(4):
                if bytes(planes[p]) != bytes(rec[p]):
                    i = next(k for k in range(len(rec[p])) if planes[p][k] != rec[p][k])
                    reason = f"plane{p}[{hex(i)}]: asm={planes[p][i]:#04x} rec={rec[p][i]:#04x}"
                    break
            if reason is None and bytes(_array(c.mem)) != arr:
                i = next(k for k in range(_ARRAY_LEN) if _array(c.mem)[k] != arr[k])
                reason = f"array[{i}]: asm={_array(c.mem)[i]:#04x} rec={arr[i]:#04x}"
            report(stats, on_result, raise_on_divergence, "particles_draw", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_exit
    cpu.hook_names[_EXIT] = "particles_draw_verify"
