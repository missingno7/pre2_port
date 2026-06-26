"""Checkpoint for the player horizontal kinematics (1030:5A0F..5A33) — first live player-FSM leaf.

This is an INLINE block inside the per-frame player update (no CALL/RET), so the hook does the recovered
integrate, writes ``[0x4F1C]`` back (only when the move commits), then JUMPS to 0x5A36 (the Y-integrate). Like
the object ``object_velocity`` block it reproduces the ASM's architectural side effects (final FLAGS via the
last ``cmp`` + the per-path instruction count) so it stays as transparent as an atomic block-swap can be.

(Demo byte-determinism is already affected upstream by the live ``object_tick`` collapse; this hook is verified
the desync-immune way — per-call shadow in verify mode + the hook-audit firing count.)

In verify mode the ASM is the oracle: the hook predicts (no mutation) and the verify-exit hook at 0x5A36
diffs the recovered ``[0x4F1C]`` against the ASM's.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.player import VIEW_TILES, X_MAX, X_MIN, _s16, player_x_integrate

from .common import report

_ENTRY = (0x1030, 0x5A0F)     # mov ax,[0x4F22]  (start of the X integrate)
_NEXT = (0x1030, 0x5A36)      # mov ax,[0x4F2A]  (the Y integrate — first instruction after the block)
_DS = 0x1A0F
_PX = 0x4F1C


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


@registry.replace(*_ENTRY, "player_x_integrate")
def player_x_integrate_hook(cpu) -> None:
    """Native replacement for the player horizontal kinematics at 1030:5A0F..5A33."""
    mem = cpu.mem
    x, xvel, cam_left = _rw(mem, _PX), _rw(mem, 0x4F22), _rw(mem, 0x8164)
    new_x = player_x_integrate(x, xvel, cam_left)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_player_pending.append(new_x)
        interpret_current_instruction_without_hook(cpu)
        return

    # Reproduce the block's regs/FLAGS/instruction-count by the ASM control-flow path (see the disasm at
    # 5A25/5A2C/5A31): the next IRQ pushes FLAGS, and the block runs 10/12/14/15 ASM insns; step() adds 1.
    nx = (x + (_s16(xvel) >> 4)) & 0xFFFF
    bound = ((cam_left + VIEW_TILES) << 4) & 0xFFFF
    if _s16(bound) <= _s16(nx):                                  # [5A27 jle] blocked at the camera edge
        cpu.set_sub_flags(bound, nx, (bound - nx) & 0xFFFF, 16)
        cpu.instruction_count += 9
    elif _s16(nx) < X_MIN:                                       # [5A2C jl] blocked at the left world edge
        cpu.set_sub_flags(nx, X_MIN, (nx - X_MIN) & 0xFFFF, 16)
        cpu.instruction_count += 11
    elif _s16(nx) >= X_MAX:                                      # [5A31 jge] blocked at the right world edge
        cpu.set_sub_flags(nx, X_MAX, (nx - X_MAX) & 0xFFFF, 16)
        cpu.instruction_count += 13
    else:                                                       # [5A33] commit (mov keeps the 5A2E cmp flags)
        b = ((_DS << 4) + _PX) & 0xFFFFF
        mem.data[b] = nx & 0xFF
        mem.data[b + 1] = (nx >> 8) & 0xFF
        cpu.set_sub_flags(nx, X_MAX, (nx - X_MAX) & 0xFFFF, 16)
        cpu.instruction_count += 14
    cpu.s.ip = _NEXT[1]


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at 0x5A36: diff the recovered prediction vs the ASM's [0x4F1C]."""

    def _verify_at_next(c) -> None:
        pending = getattr(c, "pre2_player_pending", None)
        if pending:
            pred = pending.pop()
            actual = _rw(c.mem, _PX)
            reason = None if pred == actual else f"X rec={pred:#06x} asm={actual:#06x}"
            report(stats, on_result, raise_on_divergence, "player_x_integrate", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_NEXT] = _verify_at_next
    cpu.hook_names[_NEXT] = "player_x_integrate_verify"
