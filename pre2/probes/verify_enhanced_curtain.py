"""Prove the enhanced center-out CURTAIN reveal geometry equals the faithful one (compose_curtain_planes),
byte-for-byte, at several reveal steps. Run: python pre2/probes/verify_enhanced_curtain.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np
from pre2.bridge.live_render import compose_curtain_planes
from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.transitions import apply_curtain
from pre2.recovered.render_frame import render_frame
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", "artifacts/snapshot_pre2_gameplay_20260621_185902",
                            game_root="assets", native_replacements=True)
    m, d = rt.cpu.mem, rt.dos
    rs = read_renderer_state(m, d, game_root="assets")
    pal = d.vga_palette
    src_page, dst_page = 0x0000, 0x2000
    planes = [bytearray(0x10000) for _ in range(4)]
    render_frame(rs, planes, pal, rebuild=True)
    new_frame = render_planar_rgb_from_planes(planes, src_page, pal)
    total = 0
    for cp in [0, 2, 5, 8, 10]:
        fplanes, pg = compose_curtain_planes(planes, src_page, dst_page, cp)
        faith = render_planar_rgb_from_planes(fplanes, pg, pal)[:176]
        enh = apply_curtain(np.zeros((200, 320, 3), np.uint8), new_frame, cp)[:176]
        diff = int(np.any(enh != faith, axis=2).sum())
        total += diff
        print(f"  completed_pairs={cp:2d}: enhanced curtain vs faithful = {diff}px {'OK' if diff == 0 else 'DIFF'}")
    print("ENHANCED CURTAIN GEOMETRY:", "PASS" if total == 0 else f"FAIL ({total}px)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
