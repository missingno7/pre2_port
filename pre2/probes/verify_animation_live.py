"""Live shadow-verify of the animated-tile advance controller across REAL frame sequences.

The first state-ownership proof: drives actual gameplay frames with the checkpoint verify oracle
active, so the recovered :func:`advance_animation` runs as a shadow at 1030:367D each frame and its
predicted ``[0x6BC2]``/``[0x6BD4]`` writes are diffed against the ASM's at the redraw epilogue 3717.
The ASM stays authoritative; this confirms the recovered controller would own the state identically
over many frames (advance + throttle-miss + the cycle wrap), not just on synthetic inputs.

Headless frame driver = the documented det-clock recipe (enable_sound_blaster creates rt.dos.pic;
pump the PIT IRQ each frame so the game advances out of its frame-delay loop).
"""
import sys; sys.path.insert(0, '.')

from pre2.runtime import load_pre2_snapshot
from pre2.checkpoints import enable_pre2_hook_verification
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.cpu import IF

_SNAP = 'artifacts/snapshot_pre2_gameplay_20260621_185902'
_FRAMES = 240   # ~24 real render-loop iterations (one animated-grid redraw each)


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', _SNAP, game_root='assets', native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False

    counts = {"anim_advance": [0, 0]}
    first_div = []

    def on_result(name, ok, detail):
        if name != "anim_advance":
            return
        c = counts["anim_advance"]
        c[0 if ok else 1] += 1
        if not ok and len(first_div) < 5:
            first_div.append(detail)

    enable_pre2_hook_verification(rt, on_result=on_result)

    sb = enable_sound_blaster(rt, detection_only=True); pic = rt.dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70); dos.time_source = clock  # noqa: E731
    tick = {"next": clock()}

    def frame(sc=None):
        if sc is not None:
            try:
                deliver_scancode(rt, sc, max_steps=200000)   # hold movement -> scrolling -> redraws
            except Exception:  # noqa: BLE001
                pass
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

    for _ in range(_FRAMES):
        frame(0x4D)   # hold Right -> scroll the level so the animated grid redraws every frame

    ok, div = counts["anim_advance"]
    print(f"anim_advance live shadow: frames driven={_FRAMES}  verified={ok}  divergences={div}")
    for d in first_div:
        print("  DIV", d)
    assert ok > 0, "anim_advance never fired — the animated-grid redraw was not exercised"
    print("ANIM_ADVANCE LIVE OWNERSHIP SHADOW:", "PASS" if div == 0 else "FAIL")
    return 0 if div == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
