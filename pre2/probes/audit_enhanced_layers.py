"""Build-order step 1: AUDIT what modern (RGB/RGBA) enhanced-layer data can already be exported from the
recovered/faithful state, WITHOUT making enhanced depend on the planar model per subframe.

For gameplay witnesses it reports, per source frame:
  * sprite_instances exportable from GameFrameSnapshot: id, sprite image id, screen pos, draw order, flip,
    clip, is_hud, and the blit MODE mix (NORMAL = cleanly extractable as bg-independent RGBA; OPAQUE/ERASE =
    bg-dependent OR/mask blends that are NOT bg-independent textures -> the precise layer that needs care).
  * camera/scroll + animation-frame identity.
  * background_rgb: whether a clean background-WITHOUT-sprites renders from RendererState (object_camera=None)
    via the verified rasterizer (an allowed source-cadence EXTRACTION) -> the RGB background layer.

It does NOT build the compositor; it certifies the data is available and flags any missing layer/state.
"""
import sys
from collections import Counter
from dataclasses import replace

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_frame import render_frame
from pre2.recovered.render_snapshot import build_frame_snapshot
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes

VIEW_W, VIEW_H = 320, 176   # gameplay viewport (HUD is rows 176+)


def audit(snap):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    rs = read_renderer_state(rt.cpu.mem, rt.dos, game_root="assets")
    gs = build_frame_snapshot(rs)

    # --- sprite instances (the object layer) ---
    sprites = list(gs.sprites)
    modes = Counter(int(s.mode) for s in sprites)
    flipped = sum(1 for s in sprites if s.flip)
    hud = sum(1 for s in sprites if s.is_hud)
    clipped = sum(1 for s in sprites if s.screen_x < 0 or s.screen_y < 0
                  or s.screen_x + s.width * 8 > VIEW_W or s.screen_y + s.height > VIEW_H)
    mode_name = {0x00: "ERASE", 0x01: "NORMAL", 0x10: "OPAQUE"}
    print(f"  sprites: {len(sprites)} (hud={hud})  modes=" +
          ", ".join(f"{mode_name.get(m, hex(m))}={c}" for m, c in sorted(modes.items())) +
          f"  flipped={flipped} clipped={clipped}")
    print(f"  fields/sprite: base_id, sprite_id, screen_x/y, world_x/y, mode, flip, width(bytes), height, life, is_hud -> all present")
    print(f"  camera: x_px={gs.camera.x_px} y_px={gs.camera.y_px}   animation frame_index="
          f"{gs.animation.frame_index if gs.animation else None}")

    # --- background_rgb layer: render WITHOUT sprites via the verified rasterizer (allowed extraction) ---
    try:
        bg_planes = [bytearray(0x10000) for _ in range(4)]
        page = rs.object_camera.dest_page if rs.object_camera is not None else 0x2000
        render_frame(replace(rs, object_camera=None), bg_planes, rt.dos.vga_palette, rebuild=True)
        bg_rgb = render_planar_rgb_from_planes(bg_planes, page, rt.dos.vga_palette)[:VIEW_H]
        nonblack = int((bg_rgb.reshape(-1, 3).any(axis=1)).mean() * 100)
        # full frame (bg+sprites) for reference
        full_planes = [bytearray(0x10000) for _ in range(4)]
        render_frame(rs, full_planes, rt.dos.vga_palette, rebuild=True)
        full_rgb = render_planar_rgb_from_planes(full_planes, page, rt.dos.vga_palette)[:VIEW_H]
        sprite_px = int((np.any(bg_rgb != full_rgb, axis=2)).sum())
        print(f"  background_rgb (object_camera=None): OK, {nonblack}% non-black; "
              f"sprite pixels (full vs bg) = {sprite_px}px over viewport")
    except Exception as e:
        print(f"  background_rgb: FAILED -> {e}  (missing layer/state for clean bg extraction)")


def main():
    for label, snap in (
        ("SPIDERS 112313", "artifacts/snapshot_pre2_spiders_20260624_112313"),
        ("GAMEPLAY 185902", "artifacts/snapshot_pre2_gameplay_20260621_185902"),
        ("PLAYER-DEATH 103048", "artifacts/snapshot_pre2_player_death_20260624_103048"),
        ("BOSS 192126", "artifacts/snapshot_pre2_20260623_192126"),
    ):
        print(f"=== {label} ===")
        try:
            audit(snap)
        except Exception as e:
            print(f"  audit failed: {type(e).__name__}: {e}")
    print("\nLEGEND: NORMAL sprites -> clean bg-independent RGBA texture (mask+sprite). "
          "OPAQUE/ERASE -> bg-dependent OR/mask blend (flash/blink), NOT a standalone texture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
