"""Measure PRE2's per-game-frame instruction WORK (independent of --speed), to ground the demo-clock --speed.

The demo/record loop runs ``chunk_steps = speed // present_hz`` VM instructions per presented frame, and one
chunk == exactly one 70 Hz retrace period of emulated time. So the game does one frame of real work and then
SPINS on the 44CD present-wait until the retrace boundary. If a frame's *work* exceeds ``chunk_steps`` the
game can't finish inside one present period and slows down. Thus the smallest faithful ``--speed`` is roughly
``frame_work * present_hz``.

This drives a snapshot and splits each game-frame at the 44CD present wait:
  work = (next 44CD entry ic) - (this 44CD exit ic)     # game logic between presents (no spin)
  spin = (this 44CD exit ic) - (this 44CD entry ic)      # the retrace busy-wait
and reports the distribution of `work` (+ work+spin = the full frame), so we can see what `--speed` the game
actually needs vs. what is pure wasted spin.
"""
import sys
sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.vga_timing import ALL_NODES
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
_44CD_NODES = {ip for ip in ALL_NODES if 0x44CD <= ip <= 0x44FA}


def run(snap, speed=450_000, present_hz=70, frames=400, inject=None):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    det_speed = (speed // present_hz) * present_hz
    dos.time_source = lambda: cpu.instruction_count / det_speed
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    sub_batch = 2000

    def pump():
        now = cpu.instruction_count / det_speed
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

    works, spins = [], []
    in_wait = False
    last_exit_ic = None
    entry_ic = None
    seen_frames = 0
    guard = 0
    max_guard = speed * 60  # ~60 emulated seconds cap
    while seen_frames < frames and guard < max_guard:
        if cpu.instruction_count % sub_batch == 0:
            pump()
        at_44cd = cpu.s.cs == CS and cpu.s.ip in _44CD_NODES
        if at_44cd and not in_wait:                 # entering the present wait
            in_wait = True
            entry_ic = cpu.instruction_count
            if last_exit_ic is not None:
                works.append(entry_ic - last_exit_ic)
                seen_frames += 1
        elif not at_44cd and in_wait:               # left the present wait (ret executed)
            in_wait = False
            spins.append(cpu.instruction_count - entry_ic)
            last_exit_ic = cpu.instruction_count
        cpu.step()
        guard += 1

    return works, spins


def _stats(xs):
    if not xs:
        return "n=0"
    xs = sorted(xs)
    n = len(xs)
    return (f"n={n} min={xs[0]} p50={xs[n//2]} p90={xs[min(n-1,int(n*0.9))]} "
            f"max={xs[-1]} mean={sum(xs)//n}")


def main():
    for label, snap, inject in (
        ("GAMEPLAY idle (185902)", "artifacts/snapshot_pre2_gameplay_20260621_185902", None),
        ("MENU (075918)", "artifacts/snapshot_pre2_modeselect_20260623_075918", None),
    ):
        works, spins = run(snap, frames=300)
        print(f"=== {label} ===")
        print(f"  frame WORK (instr, no spin): {_stats(works)}")
        print(f"  present SPIN (instr):        {_stats(spins)}")
        if works:
            w = sorted(works)
            p90 = w[min(len(w) - 1, int(len(w) * 0.9))]
            print(f"  -> faithful min --speed ~= p90_work*70 = {p90 * 70:,}  "
                  f"(mean_work*70 = {sum(w) // len(w) * 70:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
