"""Prove the enhanced VERTICAL fade-out projection equals the faithful one, byte-for-byte.

For a gameplay witness, render the faithful frame and apply the recovered vertical-fade compositor
(compose_vfade_planes) at several phase extents, then apply the enhanced projection (apply_vfade over the
modern composed frame) at the same extents -- they must match exactly (the cleared bands are identical black,
the uncleared middle is the parity-equal gameplay frame). Run: python pre2/probes/verify_enhanced_vfade.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np
from pre2.bridge.live_render import compose_vfade_planes
from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.compositor import compose
from pre2.enhanced.extract import extract_enhanced_frame
from pre2.enhanced.transitions import apply_vfade
from pre2.recovered.render_frame import render_frame
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", "artifacts/snapshot_pre2_gameplay_20260621_185902",
                            game_root="assets", native_replacements=True)
    m, d = rt.cpu.mem, rt.dos
    rs = read_renderer_state(m, d, game_root="assets")
    pal = d.vga_palette
    page = rs.object_camera.dest_page
    efs = extract_enhanced_frame(m, d, game_root="assets")
    planes = [bytearray(0x10000) for _ in range(4)]
    render_frame(rs, planes, pal, rebuild=True)
    total = 0
    for top, bot in [(0, 176), (40, 120), (80, 96), (88, 88)]:
        vplanes, pg = compose_vfade_planes(planes, page, top, bot)
        faithful = render_planar_rgb_from_planes(vplanes, pg, pal)[:176]
        enhanced = apply_vfade(compose(efs, None, 1.0), top, bot)[:176]
        diff = int(np.any(enhanced != faithful, axis=2).sum())
        total += diff
        print(f"  vfade top={top:3d} bot={bot:3d}: enhanced vs faithful = {diff}px {'OK' if diff == 0 else 'DIFF'}")
    print("ENHANCED VFADE PROJECTION:", "PASS" if total == 0 else f"FAIL ({total}px)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
