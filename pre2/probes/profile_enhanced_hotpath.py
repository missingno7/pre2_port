"""Profile the enhanced source-frame hot path: where does the per-source-frame cost go, and how much of it
is still FAITHFUL planar work (render_frame / deplanarize) vs native enhanced. Run:
    python pre2/probes/profile_enhanced_hotpath.py [snapshot_dir]
"""
import sys
import time
from dataclasses import replace

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np

from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.compositor import compose
from pre2.enhanced.extract import (_ID_PAL, _render_backdrop, _zero_base, VIEWPORT_H,
                                   extract_enhanced_frame)
from pre2.recovered.render_frame import render_frame
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes

SNAP = sys.argv[1] if len(sys.argv) > 1 else "artifacts/snapshot_pre2_gameplay_20260621_185902"
N = 30


def _t(fn, n=N):
    fn()  # warm
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1000.0   # ms/call


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", SNAP, game_root="assets", native_replacements=True)
    mem, dos = rt.cpu.mem, rt.dos
    rs = read_renderer_state(mem, dos, game_root="assets")
    palette = dos.vga_palette
    page = rs.object_camera.dest_page
    n_spr = len(rs.object_sprites or ())

    print(f"snapshot: {SNAP}")
    print(f"  page={page:#06x} sprites={n_spr}\n")

    # --- sub-step costs ---
    t_rs = _t(lambda: read_renderer_state(mem, dos, game_root="assets"))
    t_backdrop = _t(lambda: _render_backdrop(rs, page, palette))

    def bg0():
        bg0_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes)),
                     bg0_planes, palette, rebuild=True)
        idx0 = render_planar_rgb_from_planes(bg0_planes, page, _ID_PAL)[:, :, 0]
        return idx0 != 0
    t_bg0 = _t(bg0)

    def full_faithful():
        full_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(rs, full_planes, palette, rebuild=True)
        return render_planar_rgb_from_planes(full_planes, page, palette)
    t_faithful = _t(full_faithful)

    # whole native extract (no faithful, no effects) and its sprite portion (extract - everything-but-sprites)
    t_extract = _t(lambda: extract_enhanced_frame(mem, dos, game_root="assets", with_faithful=False))
    efs = extract_enhanced_frame(mem, dos, game_root="assets", with_faithful=False)
    t_compose = _t(lambda: compose(efs, None, 1.0))

    sprite_est = t_extract - (t_rs + t_backdrop + t_bg0)

    print("  PER-SOURCE-FRAME cost breakdown (ms, native enhanced path):")
    print(f"    read_renderer_state        {t_rs:6.2f}")
    print(f"    backdrop deplanarize       {t_backdrop:6.2f}   (faithful planar leaf)")
    print(f"    bg render+deplanarize      {t_bg0:6.2f}   (faithful planar leaf: render_frame over zeroed base)")
    print(f"    sprites (dual-paint)       {sprite_est:6.2f}   (faithful planar leaf: paint_sprite x2/sprite)")
    print(f"    ---------------------------------")
    print(f"    extract_enhanced_frame     {t_extract:6.2f}   TOTAL native source-frame extract")
    print()
    print(f"    [ref] full faithful render {t_faithful:6.2f}   (render_game_visual_state leaf; NOT in live hot path)")
    print(f"    compose (per DISPLAY frame){t_compose:6.2f}   (runs at present_hz, not source rate)")
    print()
    faithful_leaf = t_backdrop + t_bg0 + max(0.0, sprite_est)
    print(f"  faithful planar leaf work inside extract: {faithful_leaf:6.2f} ms "
          f"({100*faithful_leaf/t_extract:.0f}% of extract)")
    print(f"    -> largest single faithful dependency: "
          f"{'sprites (dual-paint)' if sprite_est>t_bg0 else 'background (render_frame+deplanarize)'}")


if __name__ == "__main__":
    main()
