"""Prove the background renders from a CLEAN framebuffer with no ASM-ring dependency.

The hidden dependency was: the opaque (type-0) tile background was built incrementally by
draw_tile_row as columns scrolled in and lived in the ASM scroll ring; render_frame's per-frame
draw_grid only redraws type>=1, so on a clean framebuffer the opaque background vanished.

build_background_ring (render_frame(..., rebuild=True)) is the explicit recovered full rebuild:
it draws every visible tile (incl. type-0) into the ring from RendererState + asset_planes only.

This probe drives a gameplay snapshot, then renders the background two ways with NO sprites:
  A) rebuild=False on the ASM-populated planes (draw_grid early-exits on a static frame -> the
     ASM's own background via the existing ring) = the reference.
  B) rebuild=True on a ZEROED framebuffer, fed only RendererState (+ asset_planes) = recovered.
It asserts A == B byte-exact over the gameplay viewport (rows 0..SCROLL_HEIGHT). The rows below
(the HUD band) are excluded — render_frame does not draw the HUD (still ASM-backed).
"""
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np
from dataclasses import replace
from pre2.runtime import load_pre2_snapshot
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt
from dos_re.cpu import IF
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_frame import render_frame
from pre2.recovered.frame_renderer import SCROLL_HEIGHT
from sdl_view import render_planar_rgb

_SNAP = 'artifacts/snapshot_pre2_gameplay_20260621_185902'


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', _SNAP, game_root='assets', native_replacements=True)
    cpu, m, dos = rt.cpu, rt.cpu.mem, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True); pic = rt.dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70); dos.time_source = clock  # noqa: E731
    tick = {"next": clock()}

    def frame():
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

    for _ in range(3):
        frame()
    ds = rt.program.memory.ega_display_start
    pal = dos.vga_palette
    rs = replace(read_renderer_state(m, dos), dest_page=ds, object_camera=None)  # no sprites

    def deplane(planes):
        buf = bytearray(len(rt.program.memory.data))
        for p in range(4):
            buf[EGA_APERTURE + p * EGA_PLANE_STRIDE:EGA_APERTURE + p * EGA_PLANE_STRIDE + len(planes[p])] = planes[p]
        return render_planar_rgb(bytes(buf), ds, pal)

    pa = [bytearray(m.data[EGA_APERTURE + p * EGA_PLANE_STRIDE:EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]) for p in range(4)]
    render_frame(rs, pa, None, rebuild=False)
    bg_asm = deplane(pa)

    pc = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]   # CLEAN framebuffer
    render_frame(rs, pc, None, rebuild=True)
    bg_rec = deplane(pc)

    mask = (np.abs(bg_asm.astype(int) - bg_rec.astype(int)).sum(2) > 0)
    vp = mask[:SCROLL_HEIGHT]
    diff = int(vp.sum())
    print(f"viewport rows 0..{SCROLL_HEIGHT}: clean-FB rebuild vs ASM-ring background diff = {diff} px")
    ok = diff == 0
    print("BACKGROUND CLEAN-FB REBUILD:", "PASS (byte-exact, no ASM-ring dependency)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
