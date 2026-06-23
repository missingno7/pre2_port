"""Verify-only checkpoint for the status-bar (HUD) draw (1030:45B8 dynamic layout -> ret 45AD).

Grounds the recovered HUD leaf :func:`pre2.recovered.hud.draw_hud` against the live ASM oracle at its
original call site — the checkpoint/verifier adapter every other visual leaf already has, which the HUD
lacked (it was covered only by a static golden test + a throwaway probe). At the HUD routine's ret the
ASM has drawn the lives / 6-digit score+trailing-0 / energy hearts / BONUS letters into page [0x2DD8];
this runs the SAME ``draw_hud`` the faithful mirror uses, onto a CLEAN framebuffer fed only the bridge
``HudState`` + the in-VM font, and diffs exactly the glyph cells ``draw_hud`` writes against the page,
byte-exact. It also transitively grounds :func:`pre2.recovered.hud.effective_bonus_mask` (the BONUS
flash-parity decision the ASM makes at 4683-46AA and draws at 46AD-46C5).

Verify-only (no ``@registry.replace`` entry): the HUD stays ASM-drawn live, because the draw is
INCREMENTAL (redraws only changed glyphs, caching last-drawn values in [0x6CA0..0x6CA7]) and writes both
pages — owning it live is a higher-risk, low-gain step, deferred. The convergence win here is that the
recovered HUD leaf is now diffed against the oracle in the standard verify pass, same impl as the mirror.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.render_state import read_hud_state
from pre2.recovered.hud import (
    HUD_BONUS_DI, HUD_ENERGY_DI, HUD_GLYPH_ROWS, HUD_LIVES_DI, HUD_MAX_HEARTS, HUD_SCORE_DI, draw_hud,
)

from .common import report

_DS = 0x1A0F
_EXIT = (0x1030, 0x45AB)     # dynamic HUD routine exit (pop es; pop ds; ret @45AD), reached via 46EB
_FONT_SEG = 0x3d             # [0x3d] = loaded chrome/font segment (glyphs at 0x1610)
_DEST_PAGE = 0x2DD8          # [0x2DD8] = the page the HUD glyphs are drawn into
_SCORE_GLYPHS = 7            # 6 digits + the fixed trailing 0 (draw_hud)
_ROW = 0x28


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _glyph_targets(hud, page):
    """The page-relative di of every glyph cell ``draw_hud`` writes for this ``HudState`` — lives, the
    7 score glyphs, the energy hearts, and each SET bonus letter. These are the regions to diff (the
    static bar background between glyphs is drawn separately, so it is excluded). The layout mirrors
    ``draw_hud``, which owns the actual pixels; this only names WHERE the leaf drew."""
    dis = [HUD_LIVES_DI]
    dis += [HUD_SCORE_DI + 2 * i for i in range(_SCORE_GLYPHS)]
    dis += [HUD_ENERGY_DI + 2 * i for i in range(HUD_MAX_HEARTS)]
    dis += [bdi for i, bdi in enumerate(HUD_BONUS_DI) if (hud.bonus_mask >> i) & 1]
    return [(d + page) & 0xFFFF for d in dis]


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the HUD verify hook at the draw routine's ret (45AB). No entry replacement — the ASM
    draws the HUD; this diffs the recovered ``draw_hud`` output against the page it just wrote."""

    def _verify_at_exit(c) -> None:
        mem = c.mem
        hud = read_hud_state(mem)
        page = _rw(mem, _DEST_PAGE)
        fontseg = _rw(mem, _FONT_SEG)
        font = bytes(mem.data[(fontseg << 4):(fontseg << 4) + 0x4000])
        rec = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
        draw_hud(rec, hud, font, page=page)
        reason = None
        for di in _glyph_targets(hud, page):
            for p in range(4):
                apbase = EGA_APERTURE + p * EGA_PLANE_STRIDE
                for row in range(HUD_GLYPH_ROWS):
                    for b in range(2):
                        off = (di + row * _ROW + b) & 0xFFFF
                        if rec[p][off] != mem.data[apbase + off]:
                            reason = (f"plane{p} di={di:#06x} row={row}: "
                                      f"asm={mem.data[apbase + off]:#04x} rec={rec[p][off]:#04x}")
                            break
                    if reason:
                        break
                if reason:
                    break
            if reason:
                break
        report(stats, on_result, raise_on_divergence, "hud_draw", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "hud_draw_verify"
