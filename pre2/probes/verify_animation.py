"""Lockstep-verify pre2.recovered.animation.advance_animation vs the ASM at 1030:367D..36A6.

Invokes the original advance block in isolation on controlled inputs (no live rendering
needed) and diffs the ASM's writes to [0x6BC2]/[0x6BD4] against the pure recovered function
across active/inactive, normal/fast, throttle-hit/miss, and the wrap boundary.
"""
import sys; sys.path.insert(0, '.')
from pre2.runtime import load_pre2_snapshot
from pre2.recovered.animation import advance_animation

_DS = 0x1A0F
_ENTRY, _ADVANCED, _SKIPPED = 0x367D, 0x36A9, 0x3717


def _ww(m, off, v):
    b = (_DS << 4) + off; m.data[b] = v & 0xFF; m.data[b + 1] = (v >> 8) & 0xFF


def _wb(m, off, v):
    m.data[(_DS << 4) + off] = v & 0xFF


def _rw(m, off):
    b = (_DS << 4) + off; return m.data[b] | (m.data[b + 1] << 8)


def _rb(m, off):
    return m.data[(_DS << 4) + off]


def run_case(rt, frame_ptr, throttle, active, speed):
    cpu, m, s = rt.cpu, rt.cpu.mem, rt.cpu.s
    _ww(m, 0x6BC2, frame_ptr); _wb(m, 0x6BD4, throttle)
    _wb(m, 0x6BBD, 1 if active else 0); _ww(m, 0x6BF6, speed)
    s.cs, s.ip, s.ds = 0x1030, _ENTRY, _DS
    for _ in range(40):
        if s.ip in (_ADVANCED, _SKIPPED):
            break
        cpu.step()
    advanced = (s.ip == _ADVANCED)
    return _rw(m, 0x6BC2), _rb(m, 0x6BD4), advanced


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_gameplay_20260621_185902',
                            game_root='assets', native_replacements=True)
    rt.cpu.trace_enabled = False
    cases = []
    for active in (False, True):
        for speed in (0x00, 0x14, 0x40):
            for fp in (0x6688, 0x6788, 0x6888):          # all 3 cycle frames (0x6888 -> wrap)
                for thr in (0x00, 0x01, 0x02, 0x03, 0x9A, 0xFF):
                    cases.append((fp, thr, active, speed))
    divs = []
    for fp, thr, active, speed in cases:
        a_ptr, a_thr, a_adv = run_case(rt, fp, thr, active, speed)
        r_ptr, r_thr, r_adv = advance_animation(fp, thr, active, speed)
        if (a_ptr, a_thr, a_adv) != (r_ptr, r_thr, r_adv):
            divs.append((fp, thr, active, speed, (a_ptr, a_thr, a_adv), (r_ptr, r_thr, r_adv)))
    print(f"advance_animation cases verified={len(cases)}  divergences={len(divs)}")
    for d in divs[:10]:
        print("  DIV", d)
    print("ADVANCE_ANIMATION LOCKSTEP:", "PASS" if not divs else "FAIL")
    return 1 if divs else 0


if __name__ == "__main__":
    sys.exit(main())
