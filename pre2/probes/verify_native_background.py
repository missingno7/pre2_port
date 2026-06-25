"""Verify the native enhanced background (layer B): native_background_indices == the faithful idx0
(render_frame over a zeroed base -> deplanarize) 0px, across static + scrolling snapshots, and report the
speedup + tile-cache hit rate. Run: python pre2/probes/verify_native_background.py
"""
import sys
import time
from dataclasses import replace

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np

from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.extract import _ID_PAL, _zero_base
from pre2.enhanced.native_background import (TileTextureCache, _HudCache, native_background_indices)
from pre2.recovered.render_frame import render_frame
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes

SNAPS = [
    "artifacts/snapshot_pre2_gameplay_20260621_185902",
    "artifacts/snapshot_pre2_gameplay_20260621_212037",
    "artifacts/snapshot_pre2_20260623_144516",
    "artifacts/snapshot_pre2_20260625_173702",
    "artifacts/snapshot_pre2_20260625_170717",
    "artifacts/snapshot_pre2_20260625_181759",
]


def _faithful_idx0(rs, page, pal):
    z = replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes))
    planes = [bytearray(0x10000) for _ in range(4)]
    render_frame(z, planes, pal, rebuild=True)
    return render_planar_rgb_from_planes(planes, page, _ID_PAL)[:, :, 0].astype(np.uint8)


def main():
    ok = True
    for snap in SNAPS:
        try:
            rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
        except Exception as e:
            print(f"  {snap.split('/')[-1]}: SKIP ({e})")
            continue
        mem, dos = rt.cpu.mem, rt.dos
        rs = read_renderer_state(mem, dos, game_root="assets")
        if rs.object_camera is None:
            print(f"  {snap.split('/')[-1]}: SKIP (no gameplay camera)")
            continue
        page, pal = rs.object_camera.dest_page, dos.vga_palette
        faith = _faithful_idx0(rs, page, pal)

        tc, hc = TileTextureCache(), _HudCache()
        native = native_background_indices(rs, tc, hc)
        diff = int(np.count_nonzero(native != faith))
        # viewport vs HUD split for clarity
        vd = int(np.count_nonzero(native[:176] != faith[:176]))
        hd = int(np.count_nonzero(native[176:] != faith[176:]))

        # timing: native (warm cache) vs faithful render
        native_background_indices(rs, tc, hc)
        t = time.perf_counter()
        for _ in range(20):
            native_background_indices(rs, tc, hc)
        nat_ms = (time.perf_counter() - t) / 20 * 1000
        t = time.perf_counter()
        for _ in range(20):
            _faithful_idx0(rs, page, pal)
        fai_ms = (time.perf_counter() - t) / 20 * 1000

        status = "OK" if diff == 0 else f"DIFF viewport={vd} hud={hd}"
        ok = ok and diff == 0
        print(f"  {snap.split('/')[-1]:42s} fine={rs.fine_scroll:3d} diff={diff:5d}px {status:22s} "
              f"native={nat_ms:5.2f}ms faithful={fai_ms:5.2f}ms  tex_hit={tc.hit_rate()*100:.0f}%")

    print("NATIVE BACKGROUND:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
