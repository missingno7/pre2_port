"""Native replacement for the OLDIES/credits glyph drawer (1030:0C3E) — one 8x12 4-plane glyph.

0C3E is the per-char leaf called by the 0C31 string loop AND the 0BEF year drawer, so replacing it grounds
`pre2.recovered.oldies_screen.blit_char` for EVERY glyph of the OLDIES easter-egg screen (message, the live
year, and the tail). Contract (from the disasm): di = row [0x2385] + x-cursor [0x2383] + page [0x2DD6];
advance the cursor [0x2383] by 1; then blit (or, for space 0x20, clear) the 8x12 4-plane glyph cell from the
font segment [0x3d] (glyph = char-0x30 if <=9 else char-0x32). ret at 0CBB.

The faithful renderer composes the same leaf via build_oldies_scene; this is its live-replacement adapter.
The cold-boot date-gated OLDIES screen isn't reached by the verify demos, so verify mode passes through to
the ASM oracle; proven instead by pre2/probes/verify_oldies_glyph.py (force-call 2417 hybrid vs ASM, Δ=0).

After the blit the routine leaves the EGA registers at the 452B base (write mode 0, function select 0, data
rotate 0); the next 0C3E re-runs 452B so the map mask is don't-care — set a benign full mask.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.recovered.oldies_screen import blit_char

_ENTRY = (0x1030, 0x0C3E)
_DATA = 0x1A0F


def _r16(d, off):
    a = ((_DATA << 4) + off) & 0xFFFFF
    return d[a] | (d[a + 1] << 8)


def _w16(d, off, val):
    a = ((_DATA << 4) + off) & 0xFFFFF
    d[a] = val & 0xFF
    d[a + 1] = (val >> 8) & 0xFF


def _planes(mem):
    mv = memoryview(mem.data)
    return [mv[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000]
            for p in range(4)]


@registry.replace(*_ENTRY, "oldies_glyph")
def oldies_glyph(cpu) -> None:
    """Mode-2 replacement at 1030:0C3E (CALL'd per char -> ret 0CBB). AL = the character.

    Live hybrid: blit the glyph via the recovered leaf, advance the x cursor, and return — skipping the
    ASM. Verify mode passes through (the cold-boot OLDIES screen isn't in the verify demos)."""
    if getattr(cpu, "pre2_verify_mode", False):
        interpret_current_instruction_without_hook(cpu)
        return
    mem = cpu.mem
    d = mem.data
    ch = cpu.s.ax & 0xFF
    cursor = _r16(d, 0x2383)
    di = (_r16(d, 0x2385) + cursor + _r16(d, 0x2DD6)) & 0xFFFF
    _w16(d, 0x2383, (cursor + 1) & 0xFFFF)            # 0C53: advance the x cursor
    font_seg = _r16(d, 0x3D)
    fbase = (font_seg << 4) & 0xFFFFF
    blit_char(_planes(mem), ch, di, bytes(d[fbase:fbase + 0x1800]))
    mem.ega_write_mode = 0
    mem.ega_map_mask = 0x0F
    mem.ega_logical_op = 0
    mem.ega_data_rotate = 0
    cpu.s.ip = cpu.pop()
