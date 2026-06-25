"""Prove the live retrace-wait PARK does not change game speed: count how many retrace waits the VM completes
per wall-second (= the game's retrace frame rate) with the park ON vs a full spin. Equal rate => same pacing.

Replicates the essential live stepping for the menu snapshot (retrace-dominated) on a perf_counter clock and
counts wait-EXIT events (transitions out of a classified wait)."""
import sys
from time import perf_counter, sleep

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.vga_timing import ALL_NODES
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
MARGIN = 0.0015
PIT_MARGIN = 0.0008
AF = 0.06
PIT_NODES = frozenset((0x1C6F, 0x1C72, 0x1C77, 0x1C79, 0x1C7B, 0x1C7E))


def _t_to_edge(now, af=AF, hz=70.0):
    phase = (now * hz) % 1.0
    thr = 1.0 - af
    return ((thr if phase < thr else 1.0) - phase) / hz


def run(snap, seconds=4.0, park=True, nodes=None, kind="retrace"):
    if nodes is None:
        nodes = ALL_NODES
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    dos.time_source = perf_counter
    dos.vga_retrace_active_fraction = AF
    tick = {"next": perf_counter()}

    def pump(n):
        now = perf_counter()
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0); tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            k = pic.acknowledge()
            if k is None:
                break
            deliver_interrupt(rt, (0x08 + k) if k < 8 else (0x70 + k - 8), max_steps=2_000_000)
            g += 1
        for _ in range(n):
            cpu.step()

    exits = 0
    slept = 0.0
    was_wait = False
    t_end = perf_counter() + seconds
    while perf_counter() < t_end:
        in_wait = cpu.s.cs == CS and cpu.s.ip in nodes
        safe = in_wait and cpu.get_flag(IF)
        pump(32)                       # equal poll granularity for park & spin -> fair exit counting
        now_wait = cpu.s.cs == CS and cpu.s.ip in nodes
        if was_wait and not now_wait:
            exits += 1
        was_wait = now_wait
        if park and safe:
            if kind == "pit":
                ev = (tick["next"] - perf_counter()) - PIT_MARGIN
            else:
                ev = _t_to_edge(perf_counter()) - MARGIN
            s = min(ev, 0.004, t_end - perf_counter())
            if s >= 0.0004:
                sleep(s); slept += s
    return exits, slept, seconds


def _report(title, snap, nodes, kind):
    ex_p, slept, secs = run(snap, park=True, nodes=nodes, kind=kind)
    ex_s, _, _ = run(snap, park=False, nodes=nodes, kind=kind)
    print(f"{title} over {secs:.0f}s wall:")
    print(f"  park ON : {ex_p} wait-exits = {ex_p / secs:5.1f}/s, CPU yielded {100 * slept / secs:.0f}%")
    print(f"  park OFF: {ex_s} wait-exits = {ex_s / secs:5.1f}/s (full spin)")
    drift = 100.0 * (ex_p - ex_s) / max(1, ex_s)
    print(f"  speed drift park-vs-spin: {drift:+.1f}%  ->  {'OK (same pacing)' if abs(drift) <= 6 else 'PACING CHANGED'}")


def main():
    _report("MENU retrace (9900)", "artifacts/snapshot_pre2_modeselect_20260623_075918", ALL_NODES, "retrace")
    _report("GAMEPLAY pit-tick (1C6F)", "artifacts/snapshot_pre2_gameplay_20260621_185902", PIT_NODES, "pit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
