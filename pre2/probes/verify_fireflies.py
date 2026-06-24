"""Lockstep-verify the firefly swarm draw (1030:54AB) vs the ASM EGA planes.

The swarm pass updates + draws in one go, OR-ing each firefly pixel into the back page. This probe drives
the firefly snapshot to the next 54AB pass, captures the EGA planes at ENTRY (baseline, before this
frame's firefly pixels), then at the RET (55FB) captures the post-update slot array and the EGA planes
again (after). The recovered ``draw_fireflies`` applied to the baseline must reproduce ``after``
byte-exact over the back page region.
"""
import sys
sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.fireflies import read_fireflies
from pre2.recovered.fireflies import draw_fireflies
from pre2.runtime import load_pre2_snapshot

_ENTRY = 0x54AB
_RET = 0x55FB


def _grab_planes(d):
    return [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
            for p in range(4)]


def main(snap="artifacts/snapshot_pre2_20260624_140330"):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = rt.dos.pic
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
    baseline = None
    for _ in range(5_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _ENTRY and baseline is None:
            baseline = _grab_planes(d)
        elif baseline is not None and s.cs == 0x1030 and s.ip == _RET:
            ff = read_fireflies(cpu.mem)
            after = _grab_planes(d)
            planes = [bytearray(b) for b in baseline]
            draw_fireflies(planes, ff.slots, ff.cam_col, ff.cam_row, ff.page)
            # Compare over the whole 0x10000 page window (firefly draws are confined to ff.page region).
            ndiff = 0
            samples = []
            for p in range(4):
                for o in range(0x10000):
                    if planes[p][o] != after[p][o]:
                        ndiff += 1
                        if len(samples) < 10:
                            samples.append((p, hex(o), hex(planes[p][o]), hex(after[p][o])))
            added = sum(sum(1 for o in range(0x10000) if after[p][o] != baseline[p][o]) for p in range(4))
            print(f"slots={len(ff.slots)} cam=({ff.cam_col},{ff.cam_row}) page={hex(ff.page)}")
            print(f"ASM added bytes (after != before): {added}")
            print(f"recovered vs ASM diff bytes: {ndiff}")
            for sm in samples:
                print("  diff", sm)
            ok = ndiff == 0
            print("FIREFLIES: PASS" if ok else "FIREFLIES: FAIL")
            return 0 if ok else 1
        cpu.step()

    print("FIREFLIES: FAIL (no 54AB pass reached)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
