"""Checkpoint for the second-pass entity projection (1030:7F26 — a clean CALL'd routine -> ret 7F6B).

`7F26` is the shared worker of the SECOND per-frame pass (the player + special-entity list at `0x8489`): it
projects an on-screen entity into a free slot of the main object list `0x4FD0` so the moving-sprite renderer
(`26FA`) draws it. Six of the pass's handlers (idx1 direct + the idx3/5-8/9/11 wrappers) call it, so hooking
this one routine makes the projection native for ALL of them while the thin wrappers stay ASM and call the
live worker.

Live hybrid: the recovered :func:`~pre2.recovered.object_inject.project_entity` OWNS the projection — on
success it writes the full contract (the projected object record, the entity mode `[entry+4]=0x17`, the
`[0xA32E]` render-pointer) and returns with CF=0; off-screen / no free slot returns CF=1 with no writes. Verify
mode keeps the ASM as oracle: shadow-predict + passthrough, diffed at the ret (`register_verify`).

This routine is COMPLETE — there are no unrecovered sub-states, so it cannot silently fall back; an unrecovered
2nd-pass *handler* (the wrappers / player FSM `7D9B`) simply runs as ASM and reaches this live worker normally.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.object_inject import (OBJ_BASE, OBJ_STRIDE, ProjectResult, find_free_object_slot,
                                          project_entity)

from .common import report

_ENTRY = (0x1030, 0x7F26)
_EXIT = (0x1030, 0x7F6B)       # the single ret (both the success `clc;ret` and the off-screen/no-slot `jb`)
_RENDER_PTR = 0xA32E           # [0xA32E] = the projected record offset (read by the anim-frame lookup at 6981)
# word fields of the projected object record, byte fields, in write order [asm 7F36..7F67]
_REC_WORDS = (0x00, 0x02, 0x04, 0x06, 0x08, 0x0A)
_REC_BYTES = (0x0E, 0x0F, 0x10)


def _predict(cpu) -> ProjectResult:
    """Run the recovered projection from the inputs at routine entry (ds:si = the 2nd-pass entry)."""
    mem, ds, si = cpu.mem, cpu.s.ds, cpu.s.si
    base = (ds << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        return mem.data[(base + (o & 0xFFFF)) & 0xFFFFF] | (mem.data[(base + ((o + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    read_id = lambda slot: rw(OBJ_BASE + slot * OBJ_STRIDE + 4)   # noqa: E731
    return project_entity(rw(si + 9), rw(si + 0xB), rw(si + 2), rb(si + 5), si,
                          rw(0x2DE4), rw(0x2DE6), lambda: find_free_object_slot(read_id))


@registry.replace(*_ENTRY, "second_pass_project_entity")
def second_pass_project_entity(cpu) -> None:
    """Native replacement for the 2nd-pass entity projection at 1030:7F26 (CALL'd -> ret 7F6B)."""
    pr = _predict(cpu)
    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_inject_pending.append((cpu.s.si, pr))
        interpret_current_instruction_without_hook(cpu)
        return

    mem, ds = cpu.mem, cpu.s.ds
    base = (ds << 4) & 0xFFFFF

    def wb(o, v):
        mem.data[(base + (o & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def ww(o, v):
        mem.data[(base + (o & 0xFFFF)) & 0xFFFFF] = v & 0xFF
        mem.data[(base + ((o + 1) & 0xFFFF)) & 0xFFFFF] = (v >> 8) & 0xFF

    if pr.drawn:
        di = OBJ_BASE + pr.slot * OBJ_STRIDE
        for off in _REC_WORDS:
            ww(di + off, pr.record[off])
        for off in _REC_BYTES:
            wb(di + off, pr.record[off])
        wb(cpu.s.si + 4, pr.mode)        # [entry+4] = 0x17
        ww(_RENDER_PTR, di)              # [0xA32E] = di (render-list pointer for the anim-frame lookup)
        cpu.s.flags &= ~0x0001           # CF=0 (drawn)
    else:
        cpu.s.flags |= 0x0001            # CF=1 (off-screen / no free slot)
    cpu.s.ip = cpu.pop()                 # near ret to the caller


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at 7F6B: diff the recovered projection vs the ASM's writes."""

    def _verify_at_ret(c) -> None:
        pending = getattr(c, "pre2_inject_pending", None)
        if pending:
            si, pr = pending.pop()
            mem, ds = c.mem, c.s.ds
            base = (ds << 4) & 0xFFFFF
            rw = lambda o: mem.data[(base + (o & 0xFFFF)) & 0xFFFFF] | (mem.data[(base + ((o + 1) & 0xFFFF)) & 0xFFFFF] << 8)  # noqa: E731
            rb = lambda o: mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]   # noqa: E731
            drawn_asm = (c.s.flags & 0x0001) == 0
            reason = None
            if pr.drawn != drawn_asm:
                reason = f"drawn rec={pr.drawn} asm={drawn_asm}"
            elif pr.drawn:
                di = OBJ_BASE + pr.slot * OBJ_STRIDE
                for off in _REC_WORDS:
                    if pr.record[off] != rw(di + off):
                        reason = f"rec[+{off:#x}] rec={pr.record[off]:#x} asm={rw(di + off):#x}"; break
                if reason is None:
                    for off in _REC_BYTES:
                        if pr.record[off] != rb(di + off):
                            reason = f"rec[+{off:#x}] rec={pr.record[off]:#x} asm={rb(di + off):#x}"; break
                if reason is None and rb(si + 4) != pr.mode:
                    reason = f"mode rec={pr.mode:#x} asm={rb(si + 4):#x}"
                if reason is None and rw(_RENDER_PTR) != di:
                    reason = f"[0xA32E] rec={di:#x} asm={rw(_RENDER_PTR):#x}"
            report(stats, on_result, raise_on_divergence, "second_pass_project_entity", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_ret
    cpu.hook_names[_EXIT] = "second_pass_project_entity_verify"
