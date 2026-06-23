"""Checkpoint for the camera-shake apply (1030:4C30, a clean CALL'd routine -> ret 4C68).

The second state-ownership proof: the recovered controller
:func:`pre2.recovered.camera_shake.apply_camera_shake` runs as a *shadow* of the ASM and its full
write contract — the renderer-visible row-stride bias ``[0x6BF8]`` (== RendererState.row_factor),
the jitter-updated magnitude ``[0x6BEA]``, and the horizontal nudge ``[0x4F1E]`` — is diffed against
the ASM's actual writes at the routine's ret. The ASM stays the oracle; this proves the recovered
controller would produce the renderer-visible shake state identically before we make it authoritative.

Verify-only for now: live hybrid is a transparent passthrough (does NOT yet own the state).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.camera_shake import apply_camera_shake

from .common import report

_DS = 0x1A0F
_ENTRY = (0x1030, 0x4C30)
_EXIT = (0x1030, 0x4C68)
_ROW_FACTOR = 0x6BF8   # [0x6BF8] vertical jolt fed to the renderer (== row_factor)
_MAGNITUDE = 0x6BEA    # [0x6BEA] shake magnitude/timer
_PARITY = 0x6BD5       # [0x6BD5] frame counter (bit 0 = parity)
_F27 = 0x4F27          # [0x4F27] state gating the horizontal nudge
_H_SCROLL = 0x4F1E     # [0x4F1E] horizontal scroll var (the -3 nudge target)


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _rb(mem, off):
    return mem.data[((_DS << 4) + off) & 0xFFFFF]


def _predict(mem):
    """Run the recovered apply from the inputs at routine entry; returns the predicted
    ``(row_factor, magnitude, h_scroll)`` the ASM should leave at the ret."""
    r = apply_camera_shake(_rw(mem, _ROW_FACTOR), _rb(mem, _MAGNITUDE), _rb(mem, _PARITY),
                           _rb(mem, _F27), _rw(mem, _H_SCROLL))
    return r.row_factor, r.magnitude, r.h_scroll


@registry.replace(*_ENTRY, "camera_shake_apply")
def camera_shake_apply(cpu) -> None:
    """Shadow checkpoint at 1030:4C30. Verify mode predicts the apply from the entry inputs; the
    ASM remains authoritative. Live hybrid = transparent passthrough (not yet owning state)."""
    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_shake_pending.append(_predict(cpu.mem))
    interpret_current_instruction_without_hook(cpu)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the apply's ret (4C68)."""

    def _verify_at_exit(c) -> None:
        if c.pre2_shake_pending:
            rf, mag, h = c.pre2_shake_pending.pop()
            a_rf, a_mag, a_h = _rw(c.mem, _ROW_FACTOR), _rb(c.mem, _MAGNITUDE), _rw(c.mem, _H_SCROLL)
            reason = None
            if a_rf != rf:
                reason = f"row_factor[6BF8]: asm={a_rf:#06x} rec={rf:#06x}"
            elif a_mag != mag:
                reason = f"magnitude[6BEA]: asm={a_mag:#04x} rec={mag:#04x}"
            elif a_h != h:
                reason = f"h_scroll[4F1E]: asm={a_h:#06x} rec={h:#06x}"
            report(stats, on_result, raise_on_divergence, "camera_shake_apply", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "camera_shake_apply_verify"
