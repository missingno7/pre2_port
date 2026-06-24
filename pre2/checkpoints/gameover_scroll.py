"""Native replacement for the game-over background present (1030:9C87) — the per-frame diorama scroll-copy.

`pre2.recovered.scene_scroll.window_scroll_copy` already let the faithful renderer COMPOSE the game-over
background; this checkpoint grounds that same leaf back into the live hybrid runtime (the architectural
correction: a recovered scene leaf that maps to an original ASM draw routine must run live, not only as a
faithful mirror). 9C87 is a write-mode-1 latched `rep movsb` of a 0x1B80-byte (176-row) window across all 4
planes, from the VRAM staging A000:(0x3F40 + 0x28*[0x6BC4]) to the back page [0x2DD8] (EGA setup 453B =
write mode 1 / map mask 0x0F). The replacement runs the recovered copy and returns, so the VM skips the ASM
rep movsb. Proven by pre2/probes/verify_gameover_scroll.py (window Δ=0). Verify mode shadows the predicted
window at entry and diffs the back-page window at the ret (9CBF) vs the ASM.

The ASM leaves the EGA registers at write mode 1 / map mask 0x0F (453B, not reset before the ret) and
touches nothing else; the replacement sets the same so later draws see identical EGA state.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.recovered.scene_scroll import window_scroll_copy

from .common import report

_ENTRY = (0x1030, 0x9C87)
_EXIT = (0x1030, 0x9CBF)
_DATA = 0x1A0F
_COUNT = 0x1B80


def _planes(mem):
    mv = memoryview(mem.data)
    return [mv[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000]
            for p in range(4)]


def _inputs(d):
    scroll = d[(_DATA << 4) + 0x6BC4]
    page = d[(_DATA << 4) + 0x2DD8] | (d[(_DATA << 4) + 0x2DD9] << 8)
    return scroll, page


@registry.replace(*_ENTRY, "gameover_scroll")
def gameover_scroll(cpu) -> None:
    """Mode-2 replacement at 1030:9C87 (CALL'd routine -> ret 9CBF).

    Live hybrid: copy the scrolled diorama window into the back page via the recovered leaf, restore the
    EGA exit state, and return — skipping the ASM rep movsb. Verify mode shadows the predicted window at
    entry and passes through so the ASM stays the oracle."""
    mem = cpu.mem
    scroll, page = _inputs(mem.data)
    planes = _planes(mem)
    if getattr(cpu, "pre2_verify_mode", False):
        src = [bytes(pl) for pl in planes]
        dst = [bytearray(b) for b in src]
        window_scroll_copy(dst, src, scroll, page)
        cpu.pre2_gameover_scroll_pending.append(
            (page, [bytes(dst[p][page:page + _COUNT]) for p in range(4)]))
        interpret_current_instruction_without_hook(cpu)
        return
    # src == dst (both the VRAM planes): staging at 0x3F40 and the back page do not overlap.
    window_scroll_copy(planes, planes, scroll, page)
    mem.ega_write_mode = 1
    mem.ega_map_mask = 0x0F
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the copy's ret (9CBF)."""

    def _verify_at_exit(c) -> None:
        if c.pre2_gameover_scroll_pending:
            page, rec = c.pre2_gameover_scroll_pending.pop()
            planes = _planes(c.mem)
            reason = None
            for p in range(4):
                asm_win = bytes(planes[p][page:page + _COUNT])
                if asm_win != rec[p]:
                    i = next(k for k in range(_COUNT) if asm_win[k] != rec[p][k])
                    reason = f"plane{p}[{hex(page + i)}]: asm={asm_win[i]:#04x} rec={rec[p][i]:#04x}"
                    break
            report(stats, on_result, raise_on_divergence, "gameover_scroll", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "gameover_scroll_verify"
