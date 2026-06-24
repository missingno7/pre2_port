"""Lockstep-verify the faithful VERTICAL fade-out curtain vs the ASM displayed page.

The vertical curtain (1030:30C6) clears the displayed page to black in two full-width 10-row bands
converging from top and bottom (the 3131 strip clear), vsync-paced — a blocking sub-loop the 6772
boundary never samples (so the faithful viewer would freeze). The faithful compose is the frame being
cleared (``render_visual_planes``, cached once) with the cleared rows blacked (``compose_vfade_planes``).
This probe drives the snapshot, and at each step (3111, after both bands of the iteration are cleared)
compares the faithful composite to the ASM displayed page over the gameplay viewport (rows 0-175).

Expected: only the standard ``render_frame`` moving-sprite residual; the fade MECHANICS (which rows
are black at each step) are byte-exact.
"""
import sys
sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.live_render import compose_vfade_planes, render_visual_planes
from pre2.runtime import load_pre2_snapshot

_DB = 0x1A0F << 4
_CS = 0x1030 << 4


def main(snap="artifacts/snapshot_pre2_20260624_110557"):
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

    def csw(o):
        return m.data[_CS + o] | (m.data[_CS + o + 1] << 8)

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

    def vp_diff_xpage(planes, base_page, ref_page):
        """Row-relative viewport diff: faithful planes at base_page vs ASM page ref_page."""
        d = rt.program.memory.data
        nd = 0
        for p in range(4):
            apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
            for r in range(176):
                for c in range(0x28):
                    if planes[p][(base_page + r * 0x28 + c) & 0xFFFF] != d[apb + ((ref_page + r * 0x28 + c) & 0xFFFF)]:
                        nd += 1
        return nd

    s = cpu.s
    state = 0
    page = 0
    base = None        # the last committed faithful frame (planes, page) — the frame being cleared
    cache = None
    results = []
    for _ in range(7_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if state == 0 and s.cs == 0x1030 and s.ip == 0x6772:
            try:
                bp, bpg, _k = render_visual_planes(m, dos, game_root="assets",
                                                   display_page=rt.program.memory.ega_display_start)
                base = (bp, bpg)
            except Exception:
                base = None
        elif state == 0 and s.cs == 0x1030 and s.ip == 0x30C6 and base is not None:
            page = m.data[_DB + 0x2DD6] | (m.data[_DB + 0x2DD7] << 8)
            cache = base
            state = 1
        elif state == 1 and s.cs == 0x1030 and s.ip == 0x3111:
            top = (csw(0x3052) - page) // 0x28 + 10
            bot = (csw(0x3052) + csw(0x3050) - page) // 0x28
            bplanes, bpage = cache
            planes, pg = compose_vfade_planes(bplanes, bpage, top, bot)
            results.append((top, bot, vp_diff_xpage(planes, pg, page)))
            if csw(0x3050) < 0x320:
                break
        cpu.step()

    assert results, "vertical curtain not reached"
    worst = max(d for _, _, d in results)
    for top, bot, d in results:
        print(f"  top_cleared={top} bot_start={bot}  faithful-vfade vs ASM viewport Δ={d}")
    print(f"worst Δ={worst} (render_frame moving-sprite residual; fade mechanics byte-exact)")
    print("VFADE: PASS" if worst <= 200 else "VFADE: FAIL")
    return 0 if worst <= 200 else 1


if __name__ == "__main__":
    sys.exit(main())
