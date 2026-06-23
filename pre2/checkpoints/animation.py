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
_FRAME_PTR = 0x6BC2    # [0x6BC2] remap pointer (the cycle state advanced here)
_THROTTLE = 0x6BD4     # [0x6BD4] per-frame throttle counter
_ACTIVE = 0x6BBD       # [0x6BBD] animated tiles present this frame (the gate)
_SPEED = 0x6BF6        # [0x6BF6] scroll speed (>=0x14 doubles the rate)


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _rb(mem, off):
    return mem.data[((_DS << 4) + off) & 0xFFFFF]


def _predict(mem):
    """Run the recovered advance from the inputs as they stand at block entry; returns the
    predicted ``([0x6BC2], [0x6BD4])`` the ASM should produce by the epilogue."""
    new_fp, new_thr, _adv = advance_animation(
        _rw(mem, _FRAME_PTR), _rb(mem, _THROTTLE), _rb(mem, _ACTIVE) != 0, _rw(mem, _SPEED))
    return new_fp, new_thr


@registry.replace(*_ENTRY, "anim_advance")
def anim_advance(cpu) -> None:
    """Shadow checkpoint at 1030:367D. Verify mode predicts the advance from the entry inputs;
    the ASM remains authoritative. Live hybrid = transparent passthrough (not yet owning state)."""
    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_anim_pending.append(_predict(cpu.mem))
    interpret_current_instruction_without_hook(cpu)


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
            report(stats, on_result, raise_on_divergence, "anim_advance", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "anim_advance_verify"
