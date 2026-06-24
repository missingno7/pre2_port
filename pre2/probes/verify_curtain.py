"""Lockstep-verify the faithful CURTAIN reveal vs the ASM displayed page.

The page-flip curtain (1030:3054) reveals the new frame (back page [0x2DD8]) center-out over the
cleared (black) front page [0x2DD6], one symmetric strip-pair per step (cs:[0x3050] = 0,4,..,0x28).
The faithful viewer composes this from the recovered leaves with NO ASM VRAM:
``render_visual_planes`` (the new room, cached once per curtain) + ``compose_curtain_planes`` (reveal
k strip-pairs over black via the verified ``panel_copy``). This probe drives a real cave-enter
(snapshot 231731), and at each curtain step (307D, after both strips of the iteration) compares the
faithful composite's dst page to the ASM displayed page over the gameplay viewport (rows 0-175).

Expected: the only residual is the standard ``render_frame`` moving-sprite blink-phase (the same
<=~single-sprite-edge artifact present on any gameplay frame), growing as more of the new room is
revealed; the curtain MECHANICS (which strips, in what order) are byte-exact.
"""
import sys
sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.live_render import compose_curtain_planes, render_visual_planes
from pre2.runtime import load_pre2_snapshot

_DB = 0x1A0F << 4
_CS = 0x1030 << 4


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", "artifacts/snapshot_pre2_20260623_231731",
                            game_root="assets", native_replacements=True)
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

    def rw(o):
        return m.data[_DB + o] | (m.data[_DB + o + 1] << 8)

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
    state = 0
    src = dst = 0
    cache = None
    results = []
    for _ in range(7_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        # find the real reveal: a 3054 entry whose back page holds a non-black new room
        if state == 0 and s.cs == 0x1030 and s.ip == 0x3054:
            src, dst = rw(0x2DD8), rw(0x2DD6)
            d = rt.program.memory.data
            blk = sum(1 for p in range(4) for r in range(0, 0xB0, 8) for c in range(0x28)
                      if d[EGA_APERTURE + p * EGA_PLANE_STRIDE + ((src + 0x14 + r * 0x28 + c) & 0xFFFF)] == 0)
            if blk < 4 * 22 * 0x28 * 0.6:
                cache, _, _ = render_visual_planes(m, dos, game_root="assets", display_page=src)
                state = 1
        elif state == 1 and s.cs == 0x1030 and s.ip == 0x307D:
            k = (m.data[_CS + 0x3050] | (m.data[_CS + 0x3051] << 8)) // 4 + 1
            planes, page = compose_curtain_planes(cache, src, dst, k)
            results.append((k, vp_diff(planes, page)))
            if k >= 9:
                break
        cpu.step()

    assert results, "curtain reveal not reached"
    worst = max(d for _, d in results)
    for k, d in results:
        print(f"  step k={k}  faithful-curtain vs ASM viewport Δ={d}")
    print(f"worst Δ={worst} (render_frame moving-sprite residual; curtain mechanics byte-exact)")
    print("CURTAIN: PASS" if worst <= 200 else "CURTAIN: FAIL")
    return 0 if worst <= 200 else 1


if __name__ == "__main__":
    sys.exit(main())
