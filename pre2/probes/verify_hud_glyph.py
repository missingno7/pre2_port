"""Lockstep-verify pre2.recovered.hud.blit_hud_glyph vs the ASM glyph blit at 1030:473D.

Invokes the original glyph blit on controlled (glyph, di) inputs into a scratch VRAM region and
diffs the four EGA planes against the pure recovered function. Proves the HUD glyph leaf is
byte-exact (the foundation for the status-bar score/lives/energy render).
"""
import sys; sys.path.insert(0, '.')
from pre2.runtime import load_pre2_snapshot
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.recovered.hud import blit_hud_glyph, HUD_GLYPH_ROWS

_DS = 0x1A0F
_BLIT = 0x473D
_RET = 0x44A0   # any address we can detect the near-ret landing on (unused code addr)


def _read_planes(mem):
    return [mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE:
                     EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE] for p in range(4)]


def run_asm(rt, glyph, di):
    cpu, m, s = rt.cpu, rt.cpu.mem, rt.cpu.s
    # clear the scratch glyph region in all planes first
    for p in range(4):
        base = EGA_APERTURE + p * EGA_PLANE_STRIDE
        for row in range(HUD_GLYPH_ROWS + 1):
            for b in range(2):
                m.data[base + ((di + row * 0x28 + b) & 0xFFFF)] = 0
    s.ax = glyph & 0xFF            # al = glyph index
    s.di = di
    s.ds = _DS                     # DGROUP, so `mov ds,[0x3d]` reads the real font segment
    # 473D assumes plain replace-mode planar writes (caller-set); set the EGA write state
    # explicitly (the snapshot left OR-mode + rotate from gameplay blits).
    rt.program.memory.ega_write_mode = 0
    rt.program.memory.ega_logical_op = 0
    rt.program.memory.ega_data_rotate = 0
    s.flags &= ~0x400              # clear DF (movsb forward)
    # push a detectable return address and enter the near routine
    s.sp = (s.sp - 2) & 0xFFFF
    ss = s.ss << 4
    m.data[ss + s.sp] = _RET & 0xFF
    m.data[ss + s.sp + 1] = (_RET >> 8) & 0xFF
    s.cs, s.ip = 0x1030, _BLIT
    for _ in range(4000):
        if s.ip == _RET and s.cs == 0x1030:
            break
        cpu.step()
    return _read_planes(m)


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_gameplay_20260621_185902',
                            game_root='assets', native_replacements=True)
    rt.cpu.trace_enabled = False
    m = rt.cpu.mem
    fontseg = m.data[(_DS << 4) + 0x3d] | (m.data[(_DS << 4) + 0x3e] << 8)
    fbase = fontseg << 4
    font = bytes(m.data[fbase:fbase + 0x4000])
    di = 0x0700                   # scratch screen offset (page 0, away from content)

    divs = []
    for glyph in (0, 1, 5, 9, 0x0A, 0x10, 0x20):
        asm = run_asm(rt, glyph, di)
        rec = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
        blit_hud_glyph(rec, glyph, di, font)
        for p in range(4):
            for row in range(HUD_GLYPH_ROWS):
                for b in range(2):
                    off = (di + row * 0x28 + b) & 0xFFFF
                    if asm[p][off] != rec[p][off]:
                        divs.append((glyph, p, row, b, asm[p][off], rec[p][off]))
                        break
    print(f"glyphs tested=7  byte divergences={len(divs)}")
    for d in divs[:8]:
        print("  DIV glyph=%#x plane=%d row=%d b=%d asm=%#x rec=%#x" % d)
    print("HUD GLYPH BLIT LOCKSTEP:", "PASS" if not divs else "FAIL")
    return 0 if not divs else 1


if __name__ == "__main__":
    sys.exit(main())
