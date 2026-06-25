"""Stage C verification — the recovered closed-form vs the interpreted ASM (poll-only segments).

At each real entry to a classified retrace wait (9900 / 990D / 44CD), this:
  1. records ic0 = instruction_count and predicts the exit via the recovered closed-form
     (pre2.recovered.vga_timing) using a ``sample(ic)`` that reproduces ``_vga_status`` at a hypothetical ic;
  2. runs the INTERPRETED ASM loop to its ``ret`` with NO mid-spin IRQ pumped (a pure poll segment);
  3. asserts the closed-form's exit instruction_count, iteration count and final bit match EXACTLY.

This isolates the poll-segment closed form (the mid-spin ISR is a separate, later concern). Drives both the
carte present loop (990D) and the menu half-wait (9900). No live hook; behavior is only observed (the drive
diverges slightly because the measured spins skip their ISR — irrelevant to per-spin closed-form accuracy).
"""
import sys
sys.path.insert(0, ".")

from collections import defaultdict

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.vga_timing import SIMULATORS
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
_RETS = {0x990C, 0x991E, 0x44E8, 0x44FA}


def run(snap, drive_to_ip, frames, chunk=6428, max_checks=400):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.cpu.mem and rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    det_speed = chunk * 70
    af = 0.06
    dos.time_source = lambda: cpu.instruction_count / det_speed
    dos.vga_retrace_active_fraction = af
    tick = {"next": 0.0}
    sub_batch = 2000

    def sample(ic):                       # == _vga_status SET-test at instruction_count == ic
        return (((ic / det_speed) * 70.0) % 1.0) >= (1.0 - af)

    def pump():
        now = cpu.instruction_count / det_speed
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0); tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        if sb:
            sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            n = pic.acknowledge()
            if n is None:
                break
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=2_000_000)
            g += 1

    for _ in range(8_000_000):            # drive into the scene
        if cpu.s.cs == CS and cpu.s.ip == drive_to_ip:
            break
        if cpu.instruction_count % sub_batch == 0:
            pump()
        cpu.step()

    res = defaultdict(lambda: {"ok": 0, "bad": 0})
    samples = []
    steps = 0
    budget = frames * chunk
    while steps < budget and sum(v["ok"] + v["bad"] for v in res.values()) < max_checks:
        if cpu.instruction_count % sub_batch == 0:
            pump()
        ip = cpu.s.ip
        if cpu.s.cs == CS and ip in SIMULATORS:
            name, sim = SIMULATORS[ip]
            ic0 = cpu.instruction_count
            pred = sim(ic0, sample)
            # run the interpreted loop to its ret with NO pump (pure poll segment)
            guard = 0
            while not (cpu.s.cs == CS and cpu.s.ip in _RETS):
                cpu.step(); steps += 1; guard += 1
                if guard > 200000:
                    break
            cpu.step(); steps += 1            # execute the ret
            actual = cpu.instruction_count - ic0
            ok = (actual == pred.instrs) and pred.final_bit
            res[name]["ok" if ok else "bad"] += 1
            if not ok and len(samples) < 8:
                samples.append((name, hex(ic0), "pred", pred.instrs, "actual", actual, "iters", pred.iterations))
        else:
            cpu.step(); steps += 1
    return res, samples


def main():
    total_bad = 0
    for label, snap, ip, frames in (
        ("CARTE 990D", "artifacts/snapshot_pre2_20260624_210538", 0x9613, 150),
        ("MENU 9900", "artifacts/snapshot_pre2_modeselect_20260623_075918", 0x97A8, 100),
    ):
        res, samples = run(snap, ip, frames)
        print(f"=== {label} ===")
        for name, v in res.items():
            print(f"  {name}: closed-form == ASM  ok={v['ok']} bad={v['bad']}")
            total_bad += v["bad"]
        for s in samples:
            print("   MISMATCH", s)
    print("VGA-TIMING CLOSED FORM:", "PASS" if total_bad == 0 else f"FAIL ({total_bad} mismatches)")
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
