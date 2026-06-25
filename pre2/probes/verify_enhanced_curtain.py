"""Sanity-check the enhanced center-out CURTAIN reveal (the smooth continuous projection of the recovered
1030:3054 panel_copy reveal -- NOT pixel-identical to the 16px strips by design, like the iris's smooth circle
vs the EGA octant). Verifies: the reveal grows monotonically center-out and at full progress equals the new
room over the viewport. Run: python pre2/probes/verify_enhanced_curtain.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np
from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.transitions import VIEWPORT_H, apply_curtain
from pre2.recovered.render_frame import render_frame
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", "artifacts/snapshot_pre2_gameplay_20260621_185902",
                            game_root="assets", native_replacements=True)
    m, d = rt.cpu.mem, rt.dos
    rs = read_renderer_state(m, d, game_root="assets")
    planes = [bytearray(0x10000) for _ in range(4)]
    render_frame(rs, planes, d.vga_palette, rebuild=True)
    new_room = render_planar_rgb_from_planes(planes, rs.object_camera.dest_page, d.vga_palette)
    widths, centred = [], True
    for p in (0.0, 0.25, 0.5, 0.75, 1.0):
        f = apply_curtain(np.zeros((200, 320, 3), np.uint8), new_room, p)
        cols = np.where((f[88] != 0).any(axis=1))[0]
        widths.append(len(cols))
        if len(cols):                                  # the revealed band must straddle the screen centre
            centred = centred and cols.min() <= 160 <= cols.max()
        print(f"  progress={p:.2f}: revealed width={len(cols):3d}px centred={len(cols) == 0 or cols.min() <= 160 <= cols.max()}")
    full = apply_curtain(np.zeros((200, 320, 3), np.uint8), new_room, 1.0)
    viewport_ok = np.array_equal(full[:VIEWPORT_H], new_room[:VIEWPORT_H])   # full progress -> the whole new room
    # widths count non-black revealed columns; the new room itself has black areas so it won't reach 320, but
    # it must grow monotonically from 0 (the band widening), and full progress must equal the new room.
    mono = widths == sorted(widths) and widths[0] == 0 and widths[-1] > widths[1]
    ok = mono and centred and viewport_ok
    print(f"  monotonic={mono} centred={centred} full==new_room(viewport)={viewport_ok}")
    print("ENHANCED CURTAIN (smooth center-out):", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
