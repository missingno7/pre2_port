"""Verify the recovered OLDIES / credits screen Δ=0 vs the ASM credits drawer.

There is no live oldies snapshot, so this probe FORCE-EXECUTES the ASM credit drawers on the title
snapshot (the font + strings are loaded): set planar mode, clear the planes, force-call 2505 (names) and
244E (header) so the ASM draws the credits to VRAM, then assert the recovered ``render_oldies`` reproduces
the same planes byte-exact.
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.oldies_scene import read_oldies
from pre2.recovered.oldies_screen import render_oldies
from pre2.runtime import load_pre2_snapshot

_DATA = 0x1A0F


def _force_call(cpu, d, entry):
    s = cpu.s
    s.ds = _DATA
    sent = 0xFFF0
    s.sp = (s.sp - 2) & 0xFFFF
    d[((s.ss << 4) + s.sp) & 0xFFFFF] = sent & 0xFF
    d[((s.ss << 4) + s.sp + 1) & 0xFFFFF] = (sent >> 8) & 0xFF
    s.cs = 0x1030
    s.ip = entry
    guard = 0
    while guard < 5_000_000:
        if s.cs == 0x1030 and s.ip == sent:
            break
        cpu.step()
        guard += 1


def main(snap=None):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*intro_image_20260622_163804")[0]
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, m, d = rt.cpu, rt.program.memory, rt.program.memory.data
    cpu.trace_enabled = False
    m.ega_planar = True
    for p in range(4):
        base = EGA_APERTURE + p * EGA_PLANE_STRIDE
        d[base:base + 0x10000] = bytes(0x10000)

    lines, font = read_oldies(cpu.mem)               # capture inputs BEFORE the ASM runs
    _force_call(cpu, d, 0x2505)                       # ASM draws the names
    _force_call(cpu, d, 0x244E)                       # ASM draws the header
    page = d[(_DATA << 4) + 0x2DD6] | (d[(_DATA << 4) + 0x2DD7] << 8)
    asm = [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
           for p in range(4)]

    planes = [bytearray(0x10000) for _ in range(4)]
    render_oldies(planes, lines, page, font)

    diff = sum(1 for p in range(4) for o in range(200 * 0x28)
               if planes[p][(page + o) & 0xFFFF] != asm[p][(page + o) & 0xFFFF])
    print(f"oldies: {len(lines)} credit lines, page={hex(page)}, recovered vs forced-ASM Δ={diff}")
    print("OLDIES: PASS" if diff == 0 else "OLDIES: FAIL")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
