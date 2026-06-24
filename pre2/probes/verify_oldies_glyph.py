"""Verify the OLDIES glyph live-replacement (1030:0C3E -> blit_char) is byte-exact vs the ASM.

The cold-boot date-gated OLDIES screen isn't reached by the verify demos, so this force-executes the OLDIES
controller (1030:2417) twice on the title snapshot (font + strings loaded): once in the HYBRID runtime
(oldies_glyph replaces 0C3E) and once as pure ASM, then asserts the drawn planes are identical.
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.runtime import load_pre2_snapshot

_DATA = 0x1A0F


def _force_2417(rt):
    cpu = rt.cpu
    d = rt.program.memory.data
    cpu.trace_enabled = False
    rt.program.memory.ega_planar = True
    for p in range(4):
        b = EGA_APERTURE + p * EGA_PLANE_STRIDE
        d[b:b + 0x10000] = bytes(0x10000)
    s = cpu.s
    s.ds = _DATA
    sent = 0xFFF0
    s.sp = (s.sp - 2) & 0xFFFF
    d[((s.ss << 4) + s.sp) & 0xFFFFF] = sent & 0xFF
    d[((s.ss << 4) + s.sp + 1) & 0xFFFFF] = (sent >> 8) & 0xFF
    s.cs = 0x1030
    s.ip = 0x2417
    g = 0
    while g < 5_000_000:
        if s.cs == 0x1030 and s.ip == sent:
            break
        cpu.step()
        g += 1
    return [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
            for p in range(4)]


def main(snap=None):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*intro_image_20260622_163804")[0]
    asm = _force_2417(load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets",
                                         native_replacements=False))
    hyb = _force_2417(load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets",
                                         native_replacements=True))
    diff = sum(1 for p in range(4) for i in range(0x10000) if asm[p][i] != hyb[p][i])
    print(f"oldies_glyph (0C3E) hybrid vs ASM: diff={diff}")
    print("OLDIES_GLYPH: PASS" if diff == 0 else "OLDIES_GLYPH: FAIL")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
