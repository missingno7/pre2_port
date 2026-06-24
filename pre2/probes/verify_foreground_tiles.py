"""Lockstep-verify the foreground-tile pass (1030:3721 + 37F7) vs the ASM EGA planes.

Drives the bush snapshot (110346) with movement so sprites are active, captures the EGA planes at the
3732 pass body entry (before any 37F7), runs the whole ASM pass to its ret (37F6), then applies the
recovered ``render_foreground_tiles`` to the before-planes and asserts it reproduces the after-planes
byte-exact over the page window.
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.foreground_tiles import read_foreground_state
from pre2.recovered.foreground_tiles import render_foreground_tiles
from pre2.runtime import load_pre2_snapshot

_BODY = 0x3732
_RET = 0x37F6


def _grab(d):
    return [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
            for p in range(4)]


def main(snap=None):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*110346")[0]
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70)  # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}
    d = rt.program.memory.data

    def pump():
        now = clock()
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0)
            tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        if sb:
            sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            nn = pic.acknowledge()
            if nn is None:
                break
            deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000)
            g += 1

    s = cpu.s
    results = []
    for i in range(8_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if i % 90000 == 0:
            deliver_scancode(rt, 0x4D)            # walk right -> active sprites
        if s.cs == 0x1030 and s.ip == _BODY:
            fg = read_foreground_state(cpu.mem)
            before = _grab(d)
            sp0 = s.sp
            guard = 0
            while guard < 2_000_000:
                cpu.step()
                guard += 1
                if s.cs == 0x1030 and s.ip == _RET and s.sp >= sp0:
                    break
            after = _grab(d)
            planes = [bytearray(b) for b in before]
            render_foreground_tiles(planes, fg)
            diff = sum(1 for p in range(4) for o in range(0x10000) if planes[p][o] != after[p][o])
            added = sum(1 for p in range(4) for o in range(0x10000) if before[p][o] != after[p][o])
            results.append((added, diff))
            if len([r for r in results if r[0] > 0]) >= 5:
                break
        cpu.step()

    nonempty = [r for r in results if r[0] > 0]
    for added, diff in results:
        print(f"  ASM added={added}  recovered diff={diff}")
    ok = bool(nonempty) and all(diff == 0 for _, diff in results)
    print(f"passes with redraws: {len(nonempty)}/{len(results)}")
    print("FOREGROUND: PASS" if ok else "FOREGROUND: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
