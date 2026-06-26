"""Checkpoint for the animated-tile cycle advance (1030:367D, inside redraw_animated_grid 3668).

This is the **first state-ownership proof**: the recovered controller
:func:`pre2.recovered.animation.advance_animation` is run as a *shadow* of the original ASM and
its predicted writes to the cycle state ``[0x6BC2]`` (remap pointer) / ``[0x6BD4]`` (throttle) are
diffed against what the ASM actually writes, **across real frame sequences**. The ASM stays the
oracle — the renderer still consumes the evolved state; this just proves the recovered advance would
produce that state identically, before we ever make it authoritative.

Block (capstone-confirmed): the gate/throttle/advance is ``367D..36A6`` (``36A6: mov [0x6BC2],ax``);
then ``36A9/36AE`` clear flags and ``36B3..3715`` is the grid redraw loop. The epilogue ``3717`` is
the **single exit reached on every path** (advance / throttle-miss / gate-fail), where both
``[0x6BC2]`` and ``[0x6BD4]`` are final — the clean verify point.

Verify-only for now: in live hybrid play this is a transparent passthrough (it does NOT yet own the
state). Making it authoritative (write the advance + skip the ASM block) is the deferred next step,
once this shadow proof has baked.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.animation import advance_animation

from .common import report

_DS = 0x1A0F
_ENTRY = (0x1030, 0x367D)
_EXIT = (0x1030, 0x3717)
_REDRAW = 0x36A9       # continuation when the cycle advanced this frame (the grid redraw)
_SKIP = 0x3665         # continuation when it did not (gate-fail / throttle-miss) -> jmp 3717 epilogue
_FRAME_PTR = 0x6BC2    # [0x6BC2] remap pointer (the cycle state advanced here)
_THROTTLE = 0x6BD4     # [0x6BD4] per-frame throttle counter
_ACTIVE = 0x6BBD       # [0x6BBD] animated tiles present this frame (the gate)
_SPEED = 0x6BF6        # [0x6BF6] scroll speed (>=0x14 doubles the rate)


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _rb(mem, off):
    return mem.data[((_DS << 4) + off) & 0xFFFFF]


def _ww(mem, off, val):
    b = ((_DS << 4) + off) & 0xFFFFF
    mem.data[b] = val & 0xFF
    mem.data[b + 1] = (val >> 8) & 0xFF


def _wb(mem, off, val):
    mem.data[((_DS << 4) + off) & 0xFFFFF] = val & 0xFF


def _run(mem):
    """Run the recovered advance from the inputs as they stand at block entry; returns
    ``(new_frame_ptr, new_throttle, advanced)``."""
    return advance_animation(
        _rw(mem, _FRAME_PTR), _rb(mem, _THROTTLE), _rb(mem, _ACTIVE) != 0, _rw(mem, _SPEED))


@registry.replace(*_ENTRY, "bg_anim_advance")
def bg_anim_advance(cpu) -> None:
    """Mode-2 replacement at 1030:367D (the cycle advance inside redraw_animated_grid 3668).

    Live hybrid: the recovered controller OWNS the cycle state — write its contract
    (``[0x6BC2]`` remap pointer, ``[0x6BD4]`` throttle) and steer the routine's control flow the way
    the ASM advance block does: into the grid redraw (``36A9``) when the cycle advanced this frame,
    else to the skip (``3665`` -> jmp 3717 epilogue). The hook sits AFTER the routine prologue
    (``3668`` pushed the regs), so it must NOT touch the stack — the ASM epilogue at 3717 pops it.
    The redraw itself stays ASM. Promoted from verify-only shadow after 0-divergence proof
    (`pre2/probes/verify_animation_live.py`). Verify mode keeps the ASM as oracle: shadow-predict +
    passthrough, diffed at the epilogue (`register_verify`)."""
    if getattr(cpu, "pre2_verify_mode", False):
        new_fp, new_thr, _adv = _run(cpu.mem)
        cpu.pre2_anim_pending.append((new_fp, new_thr))
        interpret_current_instruction_without_hook(cpu)
        return
    mem = cpu.mem
    new_fp, new_thr, advanced = _run(mem)
    _ww(mem, _FRAME_PTR, new_fp)   # [0x6BC2]
    _wb(mem, _THROTTLE, new_thr)   # [0x6BD4]
    cpu.s.ip = _REDRAW if advanced else _SKIP


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the redraw epilogue (3717)."""

    def _verify_at_exit(c) -> None:
        if c.pre2_anim_pending:
            new_fp, new_thr = c.pre2_anim_pending.pop()
            a_fp, a_thr = _rw(c.mem, _FRAME_PTR), _rb(c.mem, _THROTTLE)
            reason = None
            if a_fp != new_fp:
                reason = f"frame_ptr: asm={a_fp:#06x} rec={new_fp:#06x}"
            elif a_thr != new_thr:
                reason = f"throttle: asm={a_thr:#04x} rec={new_thr:#04x}"
            report(stats, on_result, raise_on_divergence, "bg_anim_advance", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "bg_anim_advance_verify"
