"""De-risk the native per-tile background renderer: extract palette-independent per-tile index textures from
the asset cache, then test whether the faithful idx0 (render_frame over a zeroed base -> deplanarize) is just a
SCREEN-SPACE windowing of the visible tile grid at the camera + a fine offset. Brute-forces the (voff,hoff)
fine offset and reports the residual diff -> tells us the exact geometry to reproduce. Run:
    python pre2/probes/diag_native_tiles.py [snapshot_dir]
"""
import sys
from dataclasses import replace

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np

from pre2.bridge.render_state import read_renderer_state
from pre2.enhanced.extract import _ID_PAL, _zero_base
from pre2.recovered.render_frame import render_frame
from pre2.recovered.renderer import CACHE_BASE, blit_sprite
from pre2.recovered.frame_renderer import VISIBLE_COLS, VISIBLE_ROWS
from pre2.runtime import load_pre2_snapshot
from sdl_view import render_planar_rgb_from_planes

SNAP = sys.argv[1] if len(sys.argv) > 1 else "artifacts/snapshot_pre2_gameplay_20260621_185902"
ASSET_LO = 0x5E80


def _tile_texture(asset_planes, tile_id, blit_type, mask_region):
    """The tile's over-zeroed-base 16x16 index contribution: blit it alone into a clean buffer at a canonical
    position (di=0, bg_off=0 -> a masked tile's transparent pixels read 0), then deplanarize. Palette-indep."""
    planes = [bytearray(0x10000) for _ in range(4)]
    for p in range(4):                                   # restore just the tile-graphic cache
        planes[p][ASSET_LO:ASSET_LO + len(asset_planes[p])] = asset_planes[p]
    typ = blit_type[tile_id]
    mask = mask_region[(typ - 2) * 0x20:(typ - 2) * 0x20 + 0x20] if typ >= 2 else b""
    blit_sprite(planes, tile_id, 0, typ, 0, mask)
    return render_planar_rgb_from_planes(planes, 0, _ID_PAL)[:16, :16, 0]


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", SNAP, game_root="assets", native_replacements=True)
    mem, dos = rt.cpu.mem, rt.dos
    rs = read_renderer_state(mem, dos, game_root="assets")
    page = rs.object_camera.dest_page
    pal = dos.vga_palette

    # faithful idx0 oracle (what extract uses): render_frame over a zeroed base -> indices.
    z = replace(rs, object_camera=None, asset_planes=_zero_base(rs.asset_planes))
    bg0 = [bytearray(0x10000) for _ in range(4)]
    render_frame(z, bg0, pal, rebuild=True)
    idx0 = render_planar_rgb_from_planes(bg0, page, _ID_PAL)[:, :, 0].astype(np.uint8)

    # tile-type census over the visible grid
    types = {}
    for r in range(VISIBLE_ROWS):
        for c in range(VISIBLE_COLS):
            si = (rs.camera_y * 0x100 + rs.camera_x + r * 0x100 + c) & 0xFFFF
            t = int(rs.blit_type[rs.tiles[si]])
            types[t] = types.get(t, 0) + 1
    print(f"  fine_scroll={rs.fine_scroll} col_ring={rs.col_ring} row_ring={rs.row_ring} "
          f"row_factor={rs.row_factor} scroll_src={rs.scroll_src:#06x}")
    print(f"  visible tile types: {dict(sorted(types.items()))}  (0=opaque, >=2=masked)")

    # build the 320x(VISIBLE_ROWS*16) grid by placing each tile texture in its cell
    gh = VISIBLE_ROWS * 16
    grid = np.zeros((gh, 320), np.uint8)
    texcache = {}
    for r in range(VISIBLE_ROWS):
        for c in range(VISIBLE_COLS):
            si = (rs.camera_y * 0x100 + rs.camera_x + r * 0x100 + c) & 0xFFFF
            tid = rs.tiles[si]
            gid = rs.anim_xlat[tid] if rs.flag_tbl[tid] != 0 else tid   # animated tiles -> current frame remap
            tex = texcache.get(gid)
            if tex is None:
                tex = texcache[gid] = _tile_texture(rs.asset_planes, gid, rs.blit_type, rs.mask_region)
            grid[r * 16:r * 16 + 16, c * 16:c * 16 + 16] = tex

    # brute-force the fine (voff,hoff) screen offset: idx0[:176] vs grid shifted
    best = None
    for voff in range(17):
        for hoff in range(17):
            if voff + 176 > gh:
                continue
            cand = grid[voff:voff + 176, hoff:hoff + 320] if hoff == 0 else None
            if cand is None:
                # horizontal shift needs a wider grid; skip non-zero hoff for now (cols are full width)
                continue
            d = int(np.count_nonzero(cand != idx0[:176]))
            if best is None or d < best[0]:
                best = (d, voff, hoff)
    print(f"  best screen-windowing fit: diff={best[0]}px at voff={best[1]} hoff={best[2]} "
          f"(of {176*320}=56320 px)")
    if best[0] == 0:
        print("  => idx0 IS a simple screen-space windowing of tile textures (voff=fine). Native renderer is direct.")
    else:
        voff = best[1]
        diff = (grid[voff:voff + 176, :] != idx0[:176])
        # map each differing pixel to its tile cell (row, col) and that tile's type
        by_type = {}
        cells = set()
        for (py, px) in zip(*np.nonzero(diff)):
            gr, gc = (py + voff) // 16, px // 16
            si = (rs.camera_y * 0x100 + rs.camera_x + gr * 0x100 + gc) & 0xFFFF
            t = int(rs.blit_type[rs.tiles[si]])
            by_type[t] = by_type.get(t, 0) + 1
            cells.add((gr, gc, t))
        masked = sum(n for t, n in by_type.items() if t >= 2)
        opaque = sum(n for t, n in by_type.items() if t < 2)
        print(f"  => residual {best[0]}px @ voff={voff}: diff by tile-type {dict(sorted(by_type.items()))}")
        print(f"     masked(type>=2)={masked}px  opaque(type<2)={opaque}px  across {len(cells)} cells")
        print(f"     example cells (row,col,type): {sorted(cells)[:10]}")


if __name__ == "__main__":
    main()
