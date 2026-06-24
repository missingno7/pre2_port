"""Lockstep-verify the game-over windowed scroll-copy (1030:9C87) vs the ASM VRAM.

At each 9C87 entry: snapshot the source planes (VRAM staging), the scroll [0x6BC4] and the dest page
[0x2DD8], run the ASM copy to its ret (9CBF), then assert the recovered ``window_scroll_copy`` reproduces
the dest window byte-exact.
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.scene_scroll import window_scroll_copy
from pre2.runtime import load_pre2_snapshot

_ENTRY = 0x9C87
_RET = 0x9CBF
_DATA = 0x1A0F


def _grab(d):
    return [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
            for p in range(4)]


def main(snap=None, warm=5_000_000, samples=8):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*gameover_20260623_110546")[0]
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
    while cpu.instruction_count < warm:
        if cpu.instruction_count % 1500 == 0:
            pump()
        cpu.step()

    results = []
    for _ in range(4_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _ENTRY:
            scroll = d[(_DATA << 4) + 0x6BC4]
            page = d[(_DATA << 4) + 0x2DD8] | (d[(_DATA << 4) + 0x2DD9] << 8)
            src = _grab(d)
            guard = 0
            while guard < 2_000_000:
                cpu.step()
                guard += 1
                if s.cs == 0x1030 and s.ip == _RET:    # the copy's ret (pushes already popped)
                    break
            after = _grab(d)
            dst = [bytearray(b) for b in src]               # copy into a fresh dest like the ASM
            window_scroll_copy(dst, src, scroll, page)
            diff = sum(1 for p in range(4) for i in range(0x1B80)
                       if dst[p][(page + i) & 0xFFFF] != after[p][(page + i) & 0xFFFF])
            results.append((scroll, diff))
            if len(results) >= samples:
                break
        cpu.step()

    assert results, "no 9C87 reached"
    for scroll, diff in results:
        print(f"  scroll[0x6BC4]={scroll}  window Δ={diff}")
    ok = all(diff == 0 for _, diff in results)
    print("GAMEOVER_SCROLL: PASS" if ok else "GAMEOVER_SCROLL: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
