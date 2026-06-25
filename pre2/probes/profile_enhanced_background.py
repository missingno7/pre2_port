"""Profile the SUBPARTS of the enhanced background extraction, to locate the ~5.9ms render_frame(rebuild)
waste before layer B. Splits it into: static backdrop (parallax base) vs scrolling tile/world rebuild
(ring build + animated grid + scroll-copy) vs deplanarize vs reconstruct. Run:
    python pre2/probes/profile_enhanced_background.py [snapshot_dir]
"""
import sys
import time
from dataclasses import replace

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np

from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.extract import _ID_PAL, _BASE_OFF, _render_backdrop, _zero_base, VIEWPORT_H
from pre2.recovered.frame_renderer import (build_background_ring, redraw_animated_grid, scroll_copy)
from pre2.recovered.render_frame import ASSET_LO, _TileMapView
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes

SNAP = sys.argv[1] if len(sys.argv) > 1 else "artifacts/snapshot_pre2_gameplay_20260621_185902"
N = 40


def _t(fn, n=N):
    fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1000.0


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", SNAP, game_root="assets", native_replacements=True)
    mem, dos = rt.cpu.mem, rt.dos
    rs = read_renderer_state(mem, dos, game_root="assets")
    pal = dos.vga_palette
    page = rs.object_camera.dest_page
    # the bg0 state (zeroed parallax base, no sprites) exactly as extract builds it
    z = replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes))
    tv = _TileMapView(z)

    def restore_assets(planes):
        for p in range(4):
            planes[p][ASSET_LO:ASSET_LO + len(z.asset_planes[p])] = z.asset_planes[p]

    def t_restore():
        planes = [bytearray(0x10000) for _ in range(4)]
        restore_assets(planes)

    def t_ring():
        planes = [bytearray(0x10000) for _ in range(4)]
        restore_assets(planes)
        build_background_ring(planes, tv, z.camera_x, z.camera_y, z.scroll_src, z.col_ring,
                              z.fine_scroll, z.blit_type, z.mask_region)

    def t_anim():
        planes = [bytearray(0x10000) for _ in range(4)]
        restore_assets(planes)
        redraw_animated_grid(planes, z.tiles, z.type_tbl, z.flag_tbl, z.anim_xlat, z.blit_type,
                             z.camera_x & 0xFF, z.camera_y & 0xFF, z.col_ring, z.scroll_src)

    # full ring+anim once, then time scroll_copy + deplanarize on the built planes
    planes = [bytearray(0x10000) for _ in range(4)]
    restore_assets(planes)
    build_background_ring(planes, tv, z.camera_x, z.camera_y, z.scroll_src, z.col_ring,
                          z.fine_scroll, z.blit_type, z.mask_region)
    redraw_animated_grid(planes, z.tiles, z.type_tbl, z.flag_tbl, z.anim_xlat, z.blit_type,
                         z.camera_x & 0xFF, z.camera_y & 0xFF, z.col_ring, z.scroll_src)

    def t_scrollcopy():
        scroll_copy(planes, z.scroll_src, page, z.col_ring, z.fine_scroll, z.row_ring, z.row_factor)

    def t_deplanarize():
        return render_planar_rgb_from_planes(planes, page, _ID_PAL)[:, :, 0]

    r_restore = _t(t_restore)
    r_ring = _t(t_ring) - r_restore
    r_anim = _t(t_anim) - r_restore
    r_scroll = _t(t_scrollcopy)
    r_depl = _t(t_deplanarize)
    r_backdrop = _t(lambda: _render_backdrop(rs, page, pal))

    print(f"snapshot: {SNAP}   page={page:#06x}\n")
    print("  BACKGROUND subpart cost (ms):")
    print(f"    A static backdrop (parallax base deplanarize @0x7E80)  {r_backdrop:6.2f}")
    print(f"    B/D ring rebuild  (build_background_ring: ALL tiles)   {r_ring:6.2f}")
    print(f"    B   animated grid (redraw_animated_grid)               {r_anim:6.2f}")
    print(f"    D   scroll_copy   (ring -> page)                       {r_scroll:6.2f}")
    print(f"        asset restore (memcpy 4 planes)                    {r_restore:6.2f}")
    print(f"        deplanarize page -> indices                        {r_depl:6.2f}")
    tile_world = r_ring + r_anim + r_scroll + r_restore + r_depl
    print(f"    ----------------------------------------")
    print(f"    tile/world layer total (render_frame rebuild + depl)   {tile_world:6.2f}")
    print()
    print(f"  => static backdrop (A) is {100*r_backdrop/(r_backdrop+tile_world):.0f}% of background cost; "
          f"the rest is the SCROLLING tile/world layer.")


if __name__ == "__main__":
    main()
