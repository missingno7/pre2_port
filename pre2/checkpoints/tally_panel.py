"""Native replacement for the level-end TALLY text panel (1030:51A3) — the per-frame "SCORE/% COMPLETED" panel.

`pre2.recovered.tally_panel.render_tally_panel` already let the faithful renderer compose the tally panel;
this checkpoint grounds the same leaf back into the live hybrid runtime (the recovered-leaf-first rule).
51A3 is the panel driver: it lays out "SCORE" + the score digits (4803/4780) and, via 5139, "LEVEL
COMPLETED" + the percentage + '%' (47C0), as plane-major 16x11 glyphs over the (already-cleared) page.
The count-up animation redraws it every frame, so it is an active repeated draw routine. The replacement
runs the recovered panel render and returns (ret 51DE), so the VM skips the ASM glyph blits. Proven by
pre2/probes/verify_tally_panel.py (panel rows Δ=0). Verify mode shadows the predicted panel rows at entry
and diffs them at the ret.

EGA exit state (measured at a real 51DE ret): write mode 0, map mask 0x08, function select 0, data rotate
0 — the 452B reset plus the glyph blit's last per-plane map-mask. Only the page flip follows the panel, so
this is restored purely to keep the contract exact.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.tally_panel import read_tally_panel
from pre2.recovered.tally_panel import render_tally_panel

from .common import report

_ENTRY = (0x1030, 0x51A3)
_EXIT = (0x1030, 0x51DE)
_DATA = 0x1A0F
_R0, _R1 = 12, 38          # the panel text rows (matches verify_tally_panel.py)
_ROW = 0x28


def _planes(mem):
    mv = memoryview(mem.data)
    return [mv[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000]
            for p in range(4)]


def _page(d):
    return d[(_DATA << 4) + 0x2DD8] | (d[(_DATA << 4) + 0x2DD9] << 8)


def _rows(plane, page):
    return bytes(plane[(page + _R0 * _ROW) & 0xFFFF: (page + _R1 * _ROW) & 0xFFFF])


@registry.replace(*_ENTRY, "tally_panel")
def tally_panel(cpu) -> None:
    """Mode-2 replacement at 1030:51A3 (CALL'd routine -> ret 51DE).

    Live hybrid: draw the recovered tally panel onto the page (over the ASM-cleared bg/objects), restore
    the EGA exit state, and return — skipping the ASM glyph blits. Verify mode shadows the predicted panel
    rows at entry (rendered onto a copy of the pre-panel page) and passes through so the ASM stays oracle."""
    mem = cpu.mem
    inp = read_tally_panel(mem)
    page = _page(mem.data)
    planes = _planes(mem)
    if getattr(cpu, "pre2_verify_mode", False):
        dst = [bytearray(bytes(pl)) for pl in planes]
        render_tally_panel(dst, inp.score, inp.percent, page,
                            inp.digit_font, inp.letters, inp.pct_glyph)
        cpu.pre2_tally_panel_pending.append((page, [_rows(dst[p], page) for p in range(4)]))
        interpret_current_instruction_without_hook(cpu)
        return
    render_tally_panel(planes, inp.score, inp.percent, page,
                       inp.digit_font, inp.letters, inp.pct_glyph)
    mem.ega_write_mode = 0
    mem.ega_map_mask = 0x08
    mem.ega_logical_op = 0
    mem.ega_data_rotate = 0
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the panel driver's ret (51DE)."""

    def _verify_at_exit(c) -> None:
        if c.pre2_tally_panel_pending:
            page, rec = c.pre2_tally_panel_pending.pop()
            planes = _planes(c.mem)
            reason = None
            for p in range(4):
                asm_rows = _rows(planes[p], page)
                if asm_rows != rec[p]:
                    i = next(k for k in range(len(asm_rows)) if asm_rows[k] != rec[p][k])
                    off = (page + _R0 * _ROW + i) & 0xFFFF
                    reason = f"plane{p}[{hex(off)}]: asm={asm_rows[i]:#04x} rec={rec[p][i]:#04x}"
                    break
            report(stats, on_result, raise_on_divergence, "tally_panel", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "tally_panel_verify"
