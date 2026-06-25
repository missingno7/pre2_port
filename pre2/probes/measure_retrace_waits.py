"""Stage B (measurement-only) for the timing-hook pass — quantify the VGA retrace busy-waits.

NON-INVASIVE: drives a scene on the deterministic clock exactly like the headless demo path
(_advance_demo_frame / _pump_and_step semantics: chunk_steps step()s per frame, sub_batch=2000 IRQ
batching) and OBSERVES. Changes no behavior.

Per wait loop (9900 / 990D / 44CD) it reports: entry count, spin length in instruction_count (entry->ret;
this INCLUDES any mid-spin ISR instructions, which is the whole point), the poll-iteration count, the
implied ISR instructions per spin, the share of total cpu.step()s spent inside the waits, and how many
timer IRQs are delivered while the CPU is paused mid-spin (the mid-spin IRQ problem, measured).
"""
import sys
sys.path.insert(0, ".")

from collections import defaultdict

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
# (entry ip, ret ip, poll ip(s)) per classified loop
LOOPS = {
    "9900_retrace_start": (0x9900, 0x990C, (0x9905,)),
    "990D_retrace_edge":  (0x990D, 0x991E, (0x9912, 0x9905)),   # 990D may exit via 990C (shared 9905 tail)
    "44CD_present_edge":  (0x44CD, 0x44E8, (0x44DC, 0x44E1)),   # color path
}
_ENTRIES = {ip: name for name, (ip, _r, _p) in LOOPS.items()}
_RETS = {0x990C, 0x991E, 0x44E8, 0x44FA}
_WAIT_RANGES = ((0x9900, 0x991E), (0x44CD, 0x44FA))


def _in_wait(ip):
    return any(lo <= ip <= hi for lo, hi in _WAIT_RANGES)


def main(snap="artifacts/snapshot_pre2_20260624_210538", frames=200, chunk=6428, drive_to_ip=0x9613):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos, m = rt.cpu, rt.dos, rt.cpu.mem
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    det_speed = chunk * 70
    dos.time_source = lambda: cpu.instruction_count / det_speed
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    sub_batch = 2000

    stats = defaultdict(lambda: {"entries": 0, "spin_ic": [], "polls": [], "midspin_irq": 0})
    total_steps = [0]
    steps_in_wait = [0]
    cur = {"name": None, "ic0": 0, "polls": 0}

    def pump():
        now = cpu.instruction_count / det_speed
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
            n = pic.acknowledge()
            if n is None:
                break
            if _in_wait(cpu.s.ip) and cur["name"]:        # IRQ delivered while paused mid-spin
                stats[cur["name"]]["midspin_irq"] += 1
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=2_000_000)
            g += 1

    # 1) drive into the target scene (carte present loop) before measuring
    for _ in range(8_000_000):
        if cpu.s.cs == CS and cpu.s.ip == drive_to_ip:
            break
        if cpu.instruction_count % sub_batch == 0:
            pump()
        cpu.step()

    # 2) measure `frames` frames of the deterministic budget
    for _f in range(frames):
        remaining = chunk
        while remaining > 0:
            n = min(sub_batch, remaining)
            pump()
            for _ in range(n):
                ip = cpu.s.ip
                if cpu.s.cs == CS:
                    if ip in _ENTRIES:
                        cur["name"] = _ENTRIES[ip]; cur["ic0"] = cpu.instruction_count; cur["polls"] = 0
                        stats[cur["name"]]["entries"] += 1
                    elif cur["name"] and ip in _RETS:
                        s = stats[cur["name"]]
                        s["spin_ic"].append(cpu.instruction_count - cur["ic0"])
                        s["polls"].append(cur["polls"])
                        cur["name"] = None
                    elif cur["name"] and ip in LOOPS[cur["name"]][2]:
                        cur["polls"] += 1
                    if _in_wait(ip):
                        steps_in_wait[0] += 1
                total_steps[0] += 1
                cpu.step()
            remaining -= n

    def summ(v):
        return (len(v), max(v) if v else 0, sum(v) // max(1, len(v)))

    print(f"snapshot={snap}  measured {frames} frames x {chunk} steps  (total step()s={total_steps[0]:,})")
    print(f"steps inside retrace waits: {steps_in_wait[0]:,}  = {100*steps_in_wait[0]/max(1,total_steps[0]):.1f}% of host step()s")
    print(f"PIT tick interval ~= {det_speed/max(1.0,dos.pit_channel0_hz()):.0f} instructions  (sub_batch={sub_batch} steps)")
    for name, s in stats.items():
        nspin, mx, avg = summ(s["spin_ic"])
        np_, pmx, pavg = summ(s["polls"])
        isr = avg - pavg * 3                       # spin ic minus ~3 instr/poll-iter ~= mid-spin ISR instrs
        print(f"  {name}: entries={s['entries']}  spin_ic avg={avg} max={mx}  "
              f"poll-iters avg={pavg} max={pmx}  ~ISR-instr/spin={isr}  midspin-IRQs={s['midspin_irq']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
