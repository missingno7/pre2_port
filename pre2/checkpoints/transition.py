"""Checkpoint for the end-level circular-iris transition block (1030:31F4..32B0).

The per-frame iris work is one inline block: 31F4 builds the iris-circle column table
for the current radius, 324B clears everything outside that circle (the shrinking
"darkness closing in"), falling through to 32B0. Hooking the whole block replaces the
VM's interpreted per-pixel ``clear_span`` loop with the native recovered primitives —
this is the slow part of the transition.

Thin VM contact point: read the inputs via ``pre2.bridge.transition``, run the recovered
``build_scaled_columns`` + ``draw_scale_frame``, write the cleared four EGA planes (and
the scaled-column tables) back, then continue at 32B0. The block is reached by a jump /
fall-through (not a CALL), so there is no return address to pop — only ``ip`` advances.

Live-hooked: in hybrid play the recovered iris drives the planes natively. In verify mode
the original ASM is the oracle and the recovered planes are diffed at the block exit
(32B0). Proven byte-exact over 47 live frames (pre2/probes/verify_iris_block.py).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import transition as _tr
from pre2.recovered.transition import build_scaled_columns, draw_scale_frame

from .common import report

_ENTRY = (0x1030, 0x31F4)
_EXIT = (0x1030, 0x32B0)


def _run(planes, inp):
    """Build this frame's iris-circle table and clear outside it, writing ``planes``.
    Returns ``(xs, ys, cur_y, cur_x)`` — the scaled-column tables + the draw loop's
    terminal row/column (the DGROUP scratch the ASM leaves at 32B0)."""
    xs, ys = build_scaled_columns(inp.src_x, inp.src_y, inp.scale,
                                  inp.x_off, inp.y_off, inp.x_clamp)
    tbl_x, tbl_y = list(inp.tbl_x), list(inp.tbl_y)
    for i, v in enumerate(xs):
        tbl_x[i] = v
    for i, v in enumerate(ys):
        tbl_y[i] = v
    cur_y, cur_x = draw_scale_frame(planes, tbl_x, tbl_y, len(xs),
                                    inp.x_off, inp.y_off, inp.x_clamp, inp.page)
    return xs, ys, cur_y, cur_x


@registry.replace(*_ENTRY, "iris_transition")
def iris_transition(cpu) -> None:
    """Native replacement for the iris build+clear block at 1030:31F4."""
    mem = cpu.mem
    inp = _tr.read_iris_inputs(mem)

    if getattr(cpu, "pre2_verify_mode", False):
        rec = _tr.read_planes(mem)            # copy of the before-planes
        _run(rec, inp)
        cpu.pre2_iris_pending.append(rec)
        interpret_current_instruction_without_hook(cpu)
        return

    xs, ys, cur_y, cur_x = _run(_tr.plane_views(mem), inp)  # write VRAM in place
    _tr.write_tables(mem, xs, ys)
    _tr.write_scratch(mem, xs, cur_y, cur_x)
    cpu.s.ip = _EXIT[1]                        # fall-through block: advance ip, no stack change


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at the block exit (32B0): diff the recovered
    planes (predicted at entry from the before-planes) against the ASM's planes."""

    def _verify_at_exit(c) -> None:
        if getattr(c, "pre2_iris_pending", None):
            rec = c.pre2_iris_pending.pop()
            got = _tr.read_planes(c.mem)
            reason = None
            for p in range(4):
                if rec[p] != got[p]:
                    i = next(k for k in range(len(got[p])) if got[p][k] != rec[p][k])
                    reason = f"plane{p} @ {i:#06x}: asm={got[p][i]:#04x} rec={rec[p][i]:#04x}"
                    break
            report(stats, on_result, raise_on_divergence, "iris_transition", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "iris_transition_verify"
