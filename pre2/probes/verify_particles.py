"""Lockstep-verify the faithful point-particle draw (1030:4B8E) vs the ASM displayed page.

The particles are one-shot (drawn + killed by 4B8E each frame), so the faithful renderer snapshots the
array at 4B8E ENTRY (``read_particles``) and replays the draw (``draw_particles``) onto the committed
frame. This probe drives the spider snapshot (active spider-thread particles), captures the frame at
4B8E entry, then at the next 6772 commit renders the faithful gameplay frame and compares to the ASM
displayed page over the viewport — WITHOUT particles (they show as a diff) and WITH ``draw_particles``
(the diff drops to the standard moving-sprite residual).
"""
import sys
sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.live_render import render_visual_planes
from pre2.bridge.particles import read_particles
from pre2.recovered.particles import draw_particles
from pre2.runtime import load_pre2_snapshot

_DB = 0x1A0F << 4


def main(snap="artifacts/snapshot_pre2_20260624_102733"):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos, m = rt.cpu, rt.dos, rt.cpu.mem
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = rt.dos.pic
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

    def vp_diff(planes, page):
        d = rt.program.memory.data
        nd = 0
        for p in range(4):
            apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
            for r in range(176):
                for c in range(0x28):
                    o = (page + r * 0x28 + c) & 0xFFFF
                    if planes[p][o] != d[apb + o]:
                        nd += 1
        return nd

    s = cpu.s
    results = []
    pf = None
    for _ in range(5_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == 0x4B8E:
            cand = read_particles(m)
            if cand.particles:                       # active particles this frame -> snapshot them
                pf = cand
        elif pf is not None and s.cs == 0x1030 and s.ip == 0x6772:
            disp = rt.program.memory.ega_display_start
            try:
                planes, page, _k = render_visual_planes(m, dos, game_root="assets", display_page=disp)
            except Exception:
                pf = None
                cpu.step()
                continue
            d_without = vp_diff(planes, page)
            draw_particles(planes, pf.particles, pf.cam_col, pf.cam_row, pf.y_bias, page, pf.cos, pf.sin)
            d_with = vp_diff(planes, page)
            results.append((len(pf.particles), d_without, d_with))
            pf = None
            if len(results) >= 8:
                break
        cpu.step()

    assert results, "no active-particle frame reached"
    for n, dw, dwith in results:
        print(f"  particles={n}  viewport Δ without={dw}  with draw_particles={dwith}")
    improved = sum(1 for _, dw, dwith in results if dwith < dw)
    worst_with = max(dwith for _, _, dwith in results)
    print(f"frames improved by draw_particles: {improved}/{len(results)}; worst Δ with = {worst_with}")
    ok = improved >= 1 and worst_with <= 200
    print("PARTICLES: PASS" if ok else "PARTICLES: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
