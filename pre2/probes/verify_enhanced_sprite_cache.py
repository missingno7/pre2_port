"""Verify the layer-A sprite-texture cache on a real gameplay frame:
  * EDGE/clipped sprites are exercised AND the cache path (full unclipped texture cropped by _blit) is 0px vs
    the faithful render over each clipped sprite's on-screen bbox,
  * a second extract of the same frame is ~all cache HITS (cross-frame reuse),
  * a palette change keeps L1 hitting (no re-extract) and recolours (L2 churns) -> the cached sprite RGBA
    changes colour but not coverage.
Run: python pre2/probes/verify_enhanced_sprite_cache.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np

from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.compositor import compose
from pre2.enhanced.extract import extract_enhanced_frame
from pre2.enhanced.sprite_cache import SpriteTextureCache
from pre2.recovered.object_render import MODE_NORMAL, plan_sprite, plan_sprite_command, SCREEN_H, SCREEN_W
from pre2.runtime import load_pre2_snapshot

SNAP = "artifacts/snapshot_pre2_gameplay_20260621_185902"


def _clipped_sprites(rs, cam):
    """Real on-screen bboxes of the NORMAL sprites whose draw is edge/offscreen-clipped (the case that USED to
    miss the per-frame cache every frame)."""
    attrs = rs.object_attrs or {}
    out = []
    for spr in rs.object_sprites or ():
        attr = attrs.get(spr.sprite_id)
        if attr is None:
            continue
        cmd = plan_sprite_command(spr, attr, cam)
        if cmd is None or int(cmd.mode) != MODE_NORMAL:
            continue
        d = plan_sprite(spr, attr, cam)
        if d is None:
            continue
        clipped = d.clipped or cmd.screen_x < 0 or cmd.screen_x + cmd.width > SCREEN_W \
            or cmd.screen_y < 0 or cmd.screen_y + cmd.height > SCREEN_H
        if clipped:
            x0, y0 = max(0, cmd.screen_x), max(0, cmd.screen_y)
            x1 = min(SCREEN_W, cmd.screen_x + cmd.width)
            y1 = min(SCREEN_H, cmd.screen_y + cmd.height)
            if x1 > x0 and y1 > y0:
                out.append((cmd.base_id, x0, y0, x1, y1))
    return out


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", SNAP, game_root="assets", native_replacements=True)
    mem, dos = rt.cpu.mem, rt.dos
    rs = read_renderer_state(mem, dos, game_root="assets")
    cam = rs.object_camera

    clipped = _clipped_sprites(rs, cam)
    print(f"  clipped/edge NORMAL sprites in frame: {len(clipped)}")

    cache = SpriteTextureCache()
    efs = extract_enhanced_frame(mem, dos, game_root="assets", with_faithful=True, tex_cache=cache)
    cache_frame = compose(efs, None, 1.0)                 # cache path (full unclipped texture, _blit crops)
    faithful = efs.faithful_rgb

    # 0px over each clipped sprite's on-screen bbox -> the cropped cache path matches faithful at the edges.
    edge_ok = True
    worst = 0
    for (bid, x0, y0, x1, y1) in clipped:
        d = int(np.count_nonzero(np.any(cache_frame[y0:y1, x0:x1] != faithful[y0:y1, x0:x1], axis=2)))
        worst = max(worst, d)
        edge_ok = edge_ok and d == 0
    print(f"  clipped-sprite bbox diff vs faithful: worst={worst}px  ({'OK' if edge_ok else 'MISMATCH'})")

    # second extract of the SAME frame -> all L1 hits (cross-frame reuse), no new misses.
    misses1 = cache.stats["misses"]
    extract_enhanced_frame(mem, dos, game_root="assets", with_faithful=False, tex_cache=cache)
    reuse_ok = cache.stats["misses"] == misses1
    print(f"  re-extract same frame: misses unchanged={reuse_ok} (L1 hit-rate now {cache.hit_rate()*100:.0f}%)")

    # palette change: L1 must keep hitting (no re-extract); the sprite RGBA recolours.
    spr0 = efs.sprites[0].rgba.copy() if efs.sprites else None
    faded_pal = [(min(255, r + 40), g, b) for (r, g, b) in dos.vga_palette]
    dos.vga_palette = faded_pal
    misses2 = cache.stats["misses"]
    efs2 = extract_enhanced_frame(mem, dos, game_root="assets", with_faithful=False, tex_cache=cache)
    pal_ok = cache.stats["misses"] == misses2            # palette fade did NOT re-extract
    recolor_ok = spr0 is not None and not np.array_equal(spr0[..., :3], efs2.sprites[0].rgba[..., :3]) \
        and np.array_equal(spr0[..., 3], efs2.sprites[0].rgba[..., 3])
    print(f"  palette fade: no re-extract={pal_ok}  recolours(coverage kept)={recolor_ok}")
    print(f"  {cache.summary()}")

    ok = len(clipped) > 0 and edge_ok and reuse_ok and pal_ok and recolor_ok
    print("ENHANCED SPRITE CACHE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
