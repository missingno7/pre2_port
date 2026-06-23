"""Live shadow-verify of the camera-shake APPLY controller across the fall/shake witness.

The second state-ownership proof: drives the landing snapshot (shake fires automatically within the
first frames) with the checkpoint verify oracle active, so the recovered
:func:`apply_camera_shake` runs as a shadow at 1030:4C30 each frame and its full write contract
([0x6BF8] row_factor, [0x6BEA] magnitude, [0x4F1E] h-scroll) is diffed against the ASM at the ret
(4C68). Confirms the recovered controller would produce the renderer-visible shake state identically
over the whole decay sequence (mag 0 -> set on land -> +1 jitter/parity -> decay), with the ASM as
the oracle.
"""
import sys; sys.path.insert(0, '.')

from pre2.runtime import load_pre2_snapshot
from pre2.checkpoints import enable_pre2_hook_verification
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt
from dos_re.cpu import IF

_SNAP = 'artifacts/snapshot_pre2_20260623_144516'
_FRAMES = 400   # land + the full shake decay


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', _SNAP, game_root='assets', native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False

    counts = {"camera_shake_apply": [0, 0]}
    first_div = []
    seen_mag = set()

    def on_result(name, ok, detail):
        if name != "camera_shake_apply":
            return
        c = counts["camera_shake_apply"]
        c[0 if ok else 1] += 1
        if not ok and len(first_div) < 5:
            first_div.append(detail)

    enable_pre2_hook_verification(rt, on_result=on_result)

    sb = enable_sound_blaster(rt, detection_only=True); pic = rt.dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70); dos.time_source = clock  # noqa: E731
    tick = {"next": clock()}

    def frame():
        r = 6428
        while r > 0:
            n = min(2000, r); now = clock(); tp = 1.0 / max(1.0, dos.pit_channel0_hz())
            while now >= tick["next"]:
                pic.raise_irq(0); tick["next"] += tp
                if tick["next"] < now - 0.25:
                    tick["next"] = now + tp
            if sb:
                sb.service()
            g = 0
            while cpu.get_flag(IF) and g < 64:
                nn = pic.acknowledge()
                if nn is None:
                    break
                deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000); g += 1
            for _ in range(n):
                cpu.step()
            r -= n
        seen_mag.add(cpu.mem.data[((0x1A0F << 4) + 0x6BEA) & 0xFFFFF])

    for _ in range(_FRAMES):
        frame()

    ok, div = counts["camera_shake_apply"]
    print(f"camera_shake_apply live shadow: frames driven={_FRAMES}  verified={ok}  divergences={div}")
    print(f"  magnitudes [0x6BEA] observed during drive: {sorted(seen_mag)}")
    for d in first_div:
        print("  DIV", d)
    assert ok > 0, "camera_shake_apply never fired — the shake apply was not exercised"
    assert any(mag > 1 for mag in seen_mag), "shake never became active (mag>1) — witness/drive issue"
    print("CAMERA_SHAKE_APPLY LIVE OWNERSHIP SHADOW:", "PASS" if div == 0 else "FAIL")
    return 0 if div == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
