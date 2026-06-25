"""Where does LIVE (wall-clock) gameplay spend its busy-wait time? Histogram cs:ip against the classified
retrace waits (9900/990D/44CD) and the PIT-tick spin (1C6F) under a perf_counter clock, to see what the live
cheap-wait park can actually catch."""
import sys
from collections import Counter
from time import perf_counter

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.vga_timing import ALL_NODES
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
_1C6F = set(range(0x1C6F, 0x1C80))   # PIT-tick spin neighborhood


def run(snap, seconds=4.0):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    dos.time_source = perf_counter
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": perf_counter()}

    def pump():
        now = perf_counter()
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0); tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            n = pic.acknowledge()
            if n is None:
                break
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=2_000_000)
            g += 1

    loc = Counter()
    total = 0
    t_end = perf_counter() + seconds
    batch = 0
    while perf_counter() < t_end:
        if batch % 8 == 0:
            pump()
        ip = cpu.s.ip
        if cpu.s.cs == CS:
            if ip in ALL_NODES:
                loc["retrace_wait(9900/990D/44CD)"] += 1
            elif ip in _1C6F:
                loc["pit_tick_spin(1C6F)"] += 1
            else:
                loc["other"] += 1
        else:
            loc["other_seg"] += 1
        cpu.step(); total += 1; batch += 1
    return loc, total


def main():
    for label, snap in (
        ("GAMEPLAY (185902)", "artifacts/snapshot_pre2_gameplay_20260621_185902"),
        ("MENU (075918)", "artifacts/snapshot_pre2_modeselect_20260623_075918"),
        ("CARTE/MAP (mapscroll 110253)", "artifacts/snapshot_pre2_mapscroll_20260623_110253"),
    ):
        loc, total = run(snap)
        print(f"=== {label} === ({total:,} instr in wall window)")
        for k, c in loc.most_common():
            print(f"  {k:34s} {c:>9,}  {100.0 * c / max(1, total):5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
