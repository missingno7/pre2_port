"""Checkpoint for the bitmap-font string drawer (1030:9886 — draw_string).

Thin VM contact point: read the string (``DS:BX``) + font + text state via
``pre2.bridge.text``, run the recovered :func:`draw_string`, write planes 2|3 + the
advanced pen back, and near-return. No glyph/layout logic lives here.

``draw_string`` is a CALLed subroutine, so the live path replicates its full exit
contract: planes 2|3, the pen ``[0xB1A6]``, ``bx`` advanced past the terminator, and
``ds`` restored to DGROUP (the routine reloads ``0x1A0F`` at 98F8). It fires only on the
non-gameplay scenes (title / score / tally / menu redraws), never in steady gameplay, so
it is inert in the live gameplay path.

Live-hooked: the recovered text drawer writes the planes natively. In verify mode the
original ASM is the oracle and the recovered planes 2|3 + pen are diffed at the RET (98FF).
Verified byte-exact: 24/24 menu text draws via demo replay (pre2/probes/capture_text_draw.py).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import text as _tx
from pre2.recovered.text import draw_string

from .common import report

_ENTRY = (0x1030, 0x9886)
_EXIT = (0x1030, 0x98FF)
_DGROUP = 0x1A0F


@registry.replace(*_ENTRY, "draw_string")
def draw_string_hook(cpu) -> None:
    """Native replacement for draw_string at 1030:9886."""
    mem, s = cpu.mem, cpu.s
    inp = _tx.read_text_inputs(mem, s.ds, s.bx)

    if getattr(cpu, "pre2_verify_mode", False):
        rec = _tx.read_planes(mem)
        pen = draw_string(rec, inp.text, inp.font, inp.font_base, inp.pen,
                          inp.advance, inp.page_draw, inp.page_clear)
        cpu.pre2_text_pending.append((rec[2], rec[3], pen))
        interpret_current_instruction_without_hook(cpu)
        return

    pen = draw_string(_tx.plane_views(mem), inp.text, inp.font, inp.font_base, inp.pen,
                      inp.advance, inp.page_draw, inp.page_clear)
    _tx.write_pen(mem, pen)
    s.bx = (s.bx + _tx.consumed_bytes(inp.text)) & 0xFFFF   # [asm 9888: inc bx per char + terminator]
    s.ds = _DGROUP                                          # [asm 98F8: ds reloaded to 0x1A0F]
    s.ip = cpu.pop()                                        # near ret (98FF)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at the RET (98FF): diff the recovered planes 2|3
    + advanced pen (predicted at entry) against the ASM's."""

    def _verify_at_exit(c) -> None:
        if getattr(c, "pre2_text_pending", None):
            p2, p3, pen = c.pre2_text_pending.pop()
            got = _tx.read_planes(c.mem)
            got_pen = _tx._rw(c.mem, _tx._PEN)
            reason = None
            if got[2] != p2 or got[3] != p3:
                pl = 2 if got[2] != p2 else 3
                g = got[pl]
                rp = p2 if pl == 2 else p3
                i = next(k for k in range(len(g)) if g[k] != rp[k])
                reason = f"plane{pl} @ {i:#06x}: asm={g[i]:#04x} rec={rp[i]:#04x}"
            elif got_pen != pen:
                reason = f"pen: asm={got_pen:#06x} rec={pen:#06x}"
            report(stats, on_result, raise_on_divergence, "draw_string", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "draw_string_verify"
