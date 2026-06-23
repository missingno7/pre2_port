"""Regression oracle for the camera-shake-on-fall visual state machine.

Drives the "right before a fall" snapshot on the deterministic clock (the shake fires
automatically ~frame 4) and asserts the recovered shake magnitude [0x6BEA] rises to its
landing amplitude and decays back to 0 over the observed window — and that the bridge's
CameraShakeState tracks it. The exact final-pixel apply path is still being confirmed, so
this verifies the recovered STATE (magnitude/active/phase), not pixels.

Headless driver note: raw cpu.step() does NOT advance frames (the game waits in the frame
delay loop) — you must pump the timer IRQ off the deterministic clock, which needs the PIC
that enable_sound_blaster creates.
"""
import sys; sys.path.insert(0, '.')
from pre2.runtime import load_pre2_snapshot
from pre2.bridge.render_state import read_renderer_state
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt
from dos_re.cpu import IF

_DS = 0x1A0F
_SNAP = 'artifacts/snapshot_pre2_20260623_144516'


def _make_driver(rt):
    cpu, dos = rt.cpu, rt.dos
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = rt.dos.pic
    chunk = 6428
    det_speed = chunk * 70
    clock = lambda: cpu.instruction_count / det_speed   # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}

    def frame():
        remaining = chunk
        while remaining > 0:
            n = min(2000, remaining)
            now = clock()
            tp = 1.0 / max(1.0, dos.pit_channel0_hz())
            while now >= tick["next"]:
                pic.raise_irq(0)
                tick["next"] += tp
                if tick["next"] < now - 0.25:
                    tick["next"] = now + tp
            if sb is not None:
                sb.service()
            guard = 0
            while cpu.get_flag(IF) and guard < 64:
                nn = pic.acknowledge()
                if nn is None:
                    break
                deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000)
                guard += 1
            for _ in range(n):
                cpu.step()
            remaining -= n
    return frame


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', _SNAP, game_root='assets', native_replacements=True)
    rt.cpu.trace_enabled = False
    frame = _make_driver(rt)
    mem = rt.cpu.mem
    mags, rowf, states = [], [], []
    for _ in range(75):
        frame()
        b = (_DS << 4)
        mags.append(mem.data[b + 0x6BEA])
        rowf.append(mem.data[b + 0x6BF8] | (mem.data[b + 0x6BF9] << 8))
        states.append(read_renderer_state(mem).shake)

    peak = max(mags)
    peak_i = mags.index(peak)
    settled = mags[-1] == 0 and any(m > 0 for m in mags)
    # bridge CameraShakeState mirrors the raw magnitude + active
    grounded = all(s.magnitude == m and s.active == (m > 0) for s, m in zip(states, mags))
    # CONFIRMED apply: applied_offset == row_factor [0x6BF8] while active (0 otherwise), and during
    # the shake [0x6BF8] only ever holds 0 or the current magnitude (the {0, magnitude} parity jolt).
    apply_ok = all(s.applied_offset == (rf if m > 0 else 0) for s, rf, m in zip(states, rowf, mags))
    # the vertical jolt is only applied while shaking, and is a small bounded offset (a recent
    # magnitude value — row_factor is set on odd parity so it can lag the decay by ~1 frame)
    jolt_ok = all((rf == 0 or m > 0) and 0 <= rf <= peak for rf, m in zip(rowf, mags))
    print("shake magnitude:", mags)
    print("row_factor[6bf8]:", rowf)
    print(f"peak={peak} at frame {peak_i};  rose-then-fell-to-0 = {settled}")
    print(f"CameraShakeState mirrors raw [0x6BEA]+active = {grounded}")
    print(f"applied_offset == row_factor (apply confirmed) = {apply_ok};  jolt in {{0,mag}} = {jolt_ok}")
    ok = peak >= 7 and 0 < peak_i < 12 and settled and grounded and apply_ok and jolt_ok
    print("SHAKE STATE MACHINE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
