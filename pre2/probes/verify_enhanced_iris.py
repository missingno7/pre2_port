"""Ground the enhanced circular-IRIS projection against the faithful iris (NOT pixel-identical by design --
the enhanced draws a true smooth circle, the original a cos/sin octant raster; the contract is that the
SAME recovered centre + radius produce the same visible/cleared region modulo the smooth-vs-octant edge).

For each real iris snapshot: render the faithful iris (compose_iris) and the enhanced projection (apply_iris
over the composed frame) and report the cleared-region agreement over the viewport. Run:
python pre2/probes/verify_enhanced_iris.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np
from pre2.bridge.render_state import read_renderer_state
from pre2.bridge.transition import read_iris_inputs
from pre2.enhanced.compositor import compose
from pre2.enhanced.extract import extract_enhanced_frame
from pre2.enhanced.transitions import apply_iris
from pre2.recovered.render_frame import render_frame
from pre2.recovered.transition import compose_iris
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes

_SNAPS = ["artifacts/snapshot_pre2_iris_20260623_074654",
          "artifacts/snapshot_pre2_tally_iris_20260622_002633"]


def main():
    worst = 100.0
    for snap in _SNAPS:
        rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
        m, d = rt.cpu.mem, rt.dos
        rs = read_renderer_state(m, d, game_root="assets")
        ii = read_iris_inputs(m)
        page = ii.page
        # faithful iris: cleared (turned-black) region
        planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(rs, planes, d.vga_palette, rebuild=True)
        base = render_planar_rgb_from_planes(planes, page, d.vga_palette)
        compose_iris(planes, ii.src_x, ii.src_y, ii.scale, ii.x_off, ii.y_off, ii.x_clamp,
                     ii.tbl_x, ii.tbl_y, page)
        faith = render_planar_rgb_from_planes(planes, page, d.vga_palette)
        faith_cleared = np.all(faith[:176] == 0, axis=2) & ~np.all(base[:176] == 0, axis=2)
        # enhanced iris: cleared region (centre swap: col=center_y, row=center_x)
        efs = extract_enhanced_frame(m, d, game_root="assets")
        enh = apply_iris(compose(efs, None, 1.0), efs.iris.radius, efs.iris.center_y, efs.iris.center_x)
        enh_cleared = np.all(enh[:176] == 0, axis=2) & ~np.all(base[:176] == 0, axis=2)
        agree = float((enh_cleared == faith_cleared).mean()) * 100.0
        worst = min(worst, agree)
        print(f"  {snap.split('/')[-1]}: r={efs.iris.radius} centre=(col {efs.iris.center_y},row "
              f"{efs.iris.center_x})  cleared-region agreement={agree:.1f}%")
    ok = worst >= 90.0   # the residual is the smooth-vs-octant edge ring; centre/radius must be right
    print("ENHANCED IRIS PROJECTION:", "PASS" if ok else f"FAIL (worst {worst:.1f}%)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
