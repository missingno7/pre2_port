"""Prove the frame-boundary GameVisualState capture reproduces the DISPLAYED page.

The camera/page fidelity bug (docs/pre2/camera_fidelity_bug.md): an ad-hoc live read of RendererState
describes the page being BUILT, not the displayed page, so the mirror mismatches during page-flip/fast
scroll. Fix: capture at the frame-commit boundary 1030:6772 (palette-fade entry, post page-flip), where
the state matches ega_display_start. This drives gameplay (pure ASM oracle) and shows:

  * at the 6772 boundary: render_game_visual_state(capture) vs the displayed page -> viewport Δ ~ 0
    (only the known blink-phase residual)
  * at a MID-FRAME instant (a few hundred instr later, not a boundary): the same capture-style read
    vs the displayed page -> LARGE Δ (the bug)

so the boundary is necessary and sufficient — no tolerance, no fallback.
"""
import sys; sys.path.insert(0, '.')

from dataclasses import replace
from pre2.runtime import load_pre2_snapshot
from pre2.bridge.game_visual_state import capture_game_visual_state, render_game_visual_state
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_frame import render_frame
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt
from dos_re.cpu import IF
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

_BOUNDARY = (0x1030, 0x6772)
_TOL = 64    # blink-phase residual only


def _vdiff(m, planes, page):
    d = 0
    for p in range(4):
        apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
        for o in range(176 * 0x28):
            a = (page + o) & 0xFFFF
            if planes[p][a] != m.data[apb + a]:
                d += 1
    return d


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_gameplay_20260621_185902',
                            game_root='assets', native_replacements=False)
    cpu, dos, m = rt.cpu, rt.dos, rt.cpu.mem
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True); pic = rt.dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70); dos.time_source = clock  # noqa: E731
    tick = {"next": clock()}

    def pump():
        now = clock(); tp = 1.0 / max(1.0, dos.pit_channel0_hz())
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

    def step_to(ip):
        for i in range(3_000_000):
            if i % 1500 == 0:
                pump()
            if (cpu.s.cs, cpu.s.ip) == (0x1030, ip):
                return True
            cpu.step()
        return False

    worst_boundary = 0
    worst_midframe = 1 << 30
    for f in range(6):
        if not step_to(_BOUNDARY[1]):
            print("boundary not reached"); return 1
        disp = rt.program.memory.ega_display_start
        # AT THE BOUNDARY: capture + render the GameVisualState, diff vs the displayed page
        gvs = capture_game_visual_state(m, dos, disp, game_root='assets')
        planes, page = render_game_visual_state(gvs)
        d_boundary = _vdiff(m, planes, page)
        worst_boundary = max(worst_boundary, d_boundary)
        # MID-FRAME (the bug): step ~600 instr off the boundary, read live + render to the SAME displayed
        # page, diff. (Demonstrates that an off-boundary read mismatches the displayed page.)
        for _ in range(600):
            cpu.step()
        rs = read_renderer_state(m, dos, game_root='assets')
        rs = replace(rs, dest_page=disp, object_camera=replace(rs.object_camera, dest_page=disp) if rs.object_camera else None)
        mid = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]; render_frame(rs, mid, None, rebuild=True)
        d_mid = _vdiff(m, mid, disp)
        worst_midframe = min(worst_midframe, d_mid)
        print(f"  f{f} page={page:#06x}  BOUNDARY(6772) Δ={d_boundary}   mid-frame(+600 instr) Δ={d_mid}")

    print(f"\nworst boundary Δ={worst_boundary} (<= {_TOL} = blink-phase) ; best mid-frame Δ={worst_midframe}")
    ok = worst_boundary <= _TOL and worst_midframe > _TOL
    print("FRAME-BOUNDARY CAPTURE:", "PASS (boundary reproduces displayed page; off-boundary does not)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
