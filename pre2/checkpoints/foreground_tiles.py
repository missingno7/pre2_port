"""Native replacement for the foreground-tile pass (1030:3732 -> ret 37F6).

``render_foreground_tiles`` already let the faithful renderer redraw the flag-0x40 tiles over the sprites;
this grounds the FULL pass back into the live hybrid runtime as a real ASM replacement. The pass (reached
by fall-through from the 3721 gate, so the stack holds a near return address): ``mov es,[0x2DDA]`` (the
tilemap seg) + ``452b`` (GC reset to write-mode 0 / copy), then walk the active list ``[0x4F0A]`` and, per
flag-0x40 cell in each active sprite's box, blit the tile (37F7, a colour-0-keyed transparent blit).

The replacement runs ``render_foreground_tiles`` (the verified select + blit) on the live VRAM planes, then
replicates the EGA register state the ASM leaves — always 452b (write-mode 0 / copy); and, when at least one
tile was blitted, the 37F7 blit's final plane (seq map-mask 0x08, GC read-map 3, measured constant at the
ret). It also restores ``es=[0x2DDA]`` (the pass sets it and never restores it). The pass writes only VRAM
(no DGROUP), so VRAM + EGA + es are the whole contract. Proven byte-exact (whole-state) by
pre2/probes/verify_foreground_tiles_hook.py.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge.foreground_tiles import read_foreground_state
from pre2.bridge.sprites import plane_views
from pre2.recovered.foreground_tiles import render_foreground_tiles

from .common import report

_ENTRY = (0x1030, 0x3732)
_EXIT = (0x1030, 0x37F6)
_DATA = 0x1A0F
_TILEMAP_SEG = 0x2DDA


def _plane_diff(rec, got):
    for p in range(4):
        if bytes(got[p]) != bytes(rec[p]):
            i = next(k for k in range(len(rec[p])) if got[p][k] != rec[p][k])
            return f"plane{p}[{hex(i)}]: asm={got[p][i]:#04x} rec={rec[p][i]:#04x}"
    return None


def _set_ega_exit(cpu, blits: int) -> None:
    """Replicate the EGA state the pass leaves: 452b (write-mode 0 / copy) always; the 37F7 blit's last
    plane (seq map-mask 0x08, GC read-map 3) only when a tile was blitted."""
    pw = cpu.port_writer
    pw(cpu, 0x3CE, 0x0005, 16)   # [452b] GC reg5 = 0 -> write mode 0
    pw(cpu, 0x3CE, 0x0003, 16)   # [452b] GC reg3 = 0 -> copy
    if blits:
        pw(cpu, 0x3C4, 0x0802, 16)   # [37F7] seq reg2 = 0x08 (map mask -> plane 3)
        pw(cpu, 0x3CE, 0x0304, 16)   # [37F7] GC reg4 = 3 (read map -> plane 3)


@registry.replace(*_ENTRY, "foreground_tiles")
def foreground_tiles(cpu) -> None:
    """Mode-2 replacement at 1030:3732 (fall-through pass body -> ret 37F6): live foreground redraw."""
    mem = cpu.mem
    fg = read_foreground_state(mem)
    if getattr(cpu, "pre2_verify_mode", False):
        rec = [bytearray(bytes(pl)) for pl in plane_views(mem)]
        render_foreground_tiles(rec, fg)
        cpu.pre2_foreground_pending.append(rec)
        interpret_current_instruction_without_hook(cpu)
        return
    planes = plane_views(mem)
    blits = render_foreground_tiles(planes, fg)
    _set_ega_exit(cpu, blits)
    cpu.s.es = mem.data[(_DATA << 4) + _TILEMAP_SEG] | (mem.data[(_DATA << 4) + _TILEMAP_SEG + 1] << 8)
    cpu.s.ip = cpu.pop()                            # near ret (37F6)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the pass ret (37F6): diff the predicted planes."""

    def _verify_exit(c) -> None:
        if getattr(c, "pre2_foreground_pending", None):
            rec = c.pre2_foreground_pending.pop()
            report(stats, on_result, raise_on_divergence, "foreground_tiles",
                   _plane_diff(rec, plane_views(c.mem)))
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_exit
    cpu.hook_names[_EXIT] = "foreground_tiles_verify"
