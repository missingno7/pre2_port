"""Verify the recovered TALLY text panel (1030:51A3 + leaves) Δ=0 vs the ASM text region.

The tally screen draws the black bg + object pass + the text panel. This probe drives a tally frame to
the page flip (44FB), composes the recovered panel (render_tally_panel from the bridge inputs) onto fresh
planes, and compares the PANEL ROWS (12-38, the two text lines) against the ASM back page byte-exact. The
black bg + object overlay are verified separately (compose_scene; Δ=0 outside these rows).
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.tally_panel import read_tally_panel
from pre2.recovered.tally_panel import render_tally_panel
from pre2.runtime import load_pre2_snapshot

_FLIP = 0x44FB
_DATA = 0x1A0F


def main(snap=None, warm=7_500_000, samples=6):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*tally_iris_20260622_002633")[0]
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
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
    while cpu.instruction_count < warm:
        if cpu.instruction_count % 1500 == 0:
            pump()
        cpu.step()

    results = []
    for _ in range(4_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _FLIP:
            page = d[(_DATA << 4) + 0x2DD8] | (d[(_DATA << 4) + 0x2DD9] << 8)
            inp = read_tally_panel(cpu.mem)
            planes = [bytearray(0x10000) for _ in range(4)]
            # seed the panel rows from the ASM back page so only the GLYPH writes are compared (the panel
            # draws opaque glyphs over whatever the bg/objects left; we verify the recovered glyphs match)
            for p in range(4):
                apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
                planes[p][:] = d[apb:apb + 0x10000]
            # redraw the panel on a COPY that has the bg/objects but NOT the panel: instead compare the
            # recovered panel drawn over a blank-but-for-bg plane to the ASM. Simplest sound check: draw
            # the recovered panel onto a copy of the ASM page with the panel rows cleared, then compare.
            for p in range(4):
                for r in range(12, 38):
                    for c in range(0x28):
                        planes[p][(page + r * 0x28 + c) & 0xFFFF] = 0
            render_tally_panel(planes, inp.score, inp.percent, page,
                               inp.digit_font, inp.letters, inp.pct_glyph)
            diff = 0
            for p in range(4):
                apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
                for r in range(12, 38):
                    for c in range(0x28):
                        o = (page + r * 0x28 + c) & 0xFFFF
                        if planes[p][o] != d[apb + o]:
                            diff += 1
            results.append((inp.score, inp.percent, diff))
            if len(results) >= samples:
                break
        cpu.step()

    assert results, "no tally frame reached"
    for score, pct, diff in results:
        print(f"  score={score} pct={pct}  panel-rows Δ={diff}")
    ok = all(diff == 0 for _, _, diff in results)
    print("TALLY_PANEL: PASS" if ok else "TALLY_PANEL: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
