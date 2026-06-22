"""Checkpoint for the palette fade (1030:6772 — one step of a linear DAC palette fade).

Thin VM contact point: it reads the fade state + source/target palettes through the
bridge (``pre2.bridge.palette``), runs the recovered :func:`fade_palette`, writes the
new 48-component DAC + the fade counter/flags back, and near-returns. No fade logic
lives here.

When the fade is inactive (``[6C01] | [6C02] == 0``) the routine is a no-op, exactly
as the ASM (6772-6779 falls straight through to the RET) — so this hook changes nothing
outside an active fade.

Live-hooked: in hybrid play the recovered fade drives the DAC. In verify mode the
original ASM is the oracle and the recovered DAC + flags are diffed at the RET (67D6).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import palette as _pal
from pre2.recovered.transition import fade_palette

from .common import report

_ENTRY = (0x1030, 0x6772)
_EXIT = (0x1030, 0x67D6)


def _step(mem, dos):
    """Compute + apply one fade step (or no-op if inactive). Returns the predicted
    16-colour DAC + flag contract (for verify), or ``None`` when inactive."""
    if not _pal.fade_active(mem):
        return None
    fi = _pal.read_fade_inputs(mem)
    a, b = (fi.target, fi.src) if fi.direction != 0 else (fi.src, fi.target)
    out, done = fade_palette(a, b, fi.fade_amt)
    return fi, out, done


@registry.replace(*_ENTRY, "palette_fade")
def palette_fade(cpu) -> None:
    """Native replacement for the palette fade at 1030:6772."""
    mem = cpu.mem
    dos = cpu.pre2_dos

    if getattr(cpu, "pre2_verify_mode", False):
        st = _step(mem, dos)
        if st is None:
            cpu.pre2_palette_pending.append(None)
        else:
            fi, out, done = st
            cpu.pre2_palette_pending.append(
                (fi.fade_amt, _pal.predict_dac16(out), 0 if done else 1,
                 0 if done else fi.direction))
        interpret_current_instruction_without_hook(cpu)
        return

    st = _step(mem, dos)
    if st is not None:
        fi, out, done = st
        _pal.write_dac(dos, out)
        _pal.write_fade_state(mem, fi.fade_amt, done=done,
                              direction=fi.direction, active=1)
    cpu.s.ip = cpu.pop()  # near ret (the routine preserves the caller's registers)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the fade's RET (67D6)."""

    def _verify_at_exit(c) -> None:
        if c.pre2_palette_pending:
            pred = c.pre2_palette_pending.pop()
            reason = None
            if pred is not None:
                fade_amt, dac16, c01, c02 = pred
                got = _pal.read_dac16(c.pre2_dos)
                a_c01, a_c02, a_c03 = _pal.read_fade_flags(c.mem)
                if a_c03 != fade_amt:
                    reason = f"fade_amt: asm={a_c03} rec={fade_amt}"
                elif got != dac16:
                    i = next(k for k in range(16) if got[k] != dac16[k])
                    reason = f"DAC colour {i}: asm={got[i]} rec={dac16[i]}"
                elif (a_c01, a_c02) != (c01, c02):
                    reason = f"flags: asm=({a_c01},{a_c02}) rec=({c01},{c02})"
            report(stats, on_result, raise_on_divergence, "palette_fade", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "palette_fade_verify"
