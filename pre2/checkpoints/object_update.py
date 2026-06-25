"""Checkpoint for the object-update velocity integrate (1030:6861..6873) — the first authoritative
object-system routine.

This is an INLINE block inside the update walker's per-slot loop (no CALL/RET), so the hybrid hook does the
recovered integrate, writes ``[si]``/``[si+2]`` back, then JUMPS to 0x6875 (the next block). To stay
byte-transparent to the deterministic demo clock it reproduces the ASM's exact register + instruction-count
effects: final ``ax`` = ``sar(xv,4)`` (or 0xFFFF on the sentinel), and it advances ``instruction_count`` by
the same 6 (sentinel path) or 8 (full path) instructions the ASM would run. Verified: replaying a demo with
the hook gives a byte-identical per-frame hash stream (PRE2_FRAME_HASH) to the pure-ASM replay.

In verify mode the ASM is the oracle: the hook predicts (no mutation) and the verify-exit hook at 0x6875
diffs predicted vs actual ``[si]``/``[si+2]``.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.object_update import NO_X_MOVE, apply_velocity

from .common import report

_ENTRY = (0x1030, 0x6861)     # mov ax,[si+0xA]  (start of the velocity integrate)
_NEXT = (0x1030, 0x6875)      # mov bx,[si+6]    (first instruction after the block)


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _slot(cpu):
    """(linear base of the current object record, x, y, xvel, yvel) at the live DS:SI."""
    ds, si = cpu.s.ds, cpu.s.si
    base = (ds << 4) & 0xFFFFF
    def rd(o):
        a = (base + ((si + o) & 0xFFFF)) & 0xFFFFF
        return cpu.mem.data[a] | (cpu.mem.data[a + 1] << 8)
    return base, si, rd(0), rd(2), rd(8), rd(0xA)


@registry.replace(*_ENTRY, "object_velocity")
def object_velocity(cpu) -> None:
    """Native replacement for the object velocity-integrate at 1030:6861..6873."""
    base, si, x, y, xv, yv = _slot(cpu)
    nx, ny = apply_velocity(x, y, xv, yv)

    if getattr(cpu, "pre2_verify_mode", False):
        # Shadow: stash the prediction, let the ASM integrate (oracle); the verify-exit hook diffs at 6875.
        if not hasattr(cpu, "pre2_velocity_pending"):
            cpu.pre2_velocity_pending = {}
        cpu.pre2_velocity_pending[si] = (nx, ny)
        interpret_current_instruction_without_hook(cpu)
        return

    def wr(o, v):
        a = (base + ((si + o) & 0xFFFF)) & 0xFFFFF
        cpu.mem.data[a] = v & 0xFF
        cpu.mem.data[a + 1] = (v >> 8) & 0xFF
    wr(0, nx)
    wr(2, ny)
    # Reproduce the ASM's architectural side effects so the replacement stays as transparent as an atomic
    # block-swap can be (regs + FLAGS + instruction count): the next IRQ delivered after this block pushes
    # FLAGS, and the ASM left ax = the integrated X delta (0xFFFF on the sentinel). The block ran 6 ASM
    # instructions on the sentinel path (je taken) or 8 (full path); step() adds 1, so add 5 / 7.
    if xv == NO_X_MOVE:
        cpu.s.ax = NO_X_MOVE
        cpu.set_sub_flags(NO_X_MOVE, NO_X_MOVE, 0, 16)        # final op = cmp ax,0xFFFF (equal)
        cpu.instruction_count += 5
    else:
        b = (_s16(xv) >> 4) & 0xFFFF
        cpu.s.ax = b
        cpu.set_add_flags(x, b, nx, 16)                        # final op = add [si],ax
        cpu.instruction_count += 7
    cpu.s.ip = _NEXT[1]


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at 0x6875: diff the recovered prediction vs the ASM's integrate."""

    def _verify_at_next(c) -> None:
        pending = getattr(c, "pre2_velocity_pending", None)
        if pending:
            si = c.s.si
            if si in pending:
                nx, ny = pending.pop(si)
                base = (c.s.ds << 4) & 0xFFFFF
                ax = c.mem.data[(base + si) & 0xFFFFF] | (c.mem.data[(base + si + 1) & 0xFFFFF] << 8)
                ay = c.mem.data[(base + ((si + 2) & 0xFFFF)) & 0xFFFFF] \
                    | (c.mem.data[(base + ((si + 3) & 0xFFFF)) & 0xFFFFF] << 8)
                reason = None if (ax == nx and ay == ny) else \
                    f"slot {si:04X}: asm=({ax},{ay}) rec=({nx},{ny})"
                report(stats, on_result, raise_on_divergence, "object_velocity", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_NEXT] = _verify_at_next
    cpu.hook_names[_NEXT] = "object_velocity_verify"
