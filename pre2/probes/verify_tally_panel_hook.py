"""Drive the tally snapshot under enable_pre2_hook_verification and report the tally_panel checkpoint —
proves the LIVE replacement adapter (1030:51A3) matches the ASM panel at its ret (51DE), the way the
hybrid runtime installs it (verify_tally_panel.py covers the standalone leaf).
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.checkpoints import enable_pre2_hook_verification
from pre2.runtime import load_pre2_snapshot

_ENTRY = 0x51A3


def main(snap=None, warm=7_500_000, samples=6):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*tally_iris_20260622_002633")[0]
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    stats = enable_pre2_hook_verification(rt, raise_on_divergence=False)
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70)  # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}

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

    seen = 0
    for _ in range(8_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _ENTRY:
            seen += 1
            if seen >= samples:
                for _ in range(200000):
                    cpu.step()
                break
        cpu.step()

    bad = [r for r in stats.diverged if r[0] == "tally_panel"]
    print(f"tally_panel: entries seen={seen}, tally_panel diverged={len(bad)}")
    for name, reason in bad[:5]:
        print("  DIVERGE:", reason)
    ok = seen > 0 and not bad
    print("TALLY_PANEL_HOOK: PASS" if ok else "TALLY_PANEL_HOOK: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
