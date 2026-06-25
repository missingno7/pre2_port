"""Native enhanced background (layer B) — produce the gameplay background indices (``idx0``) directly from the
recovered tilemap + tile-graphic cache + camera, WITHOUT the per-frame ``render_frame(rebuild)`` ring rebuild
(the ~5.9ms hot-path cost the enhanced extractor used to pay).

Validated geometry (``pre2/probes/diag_native_tiles.py``, 0px across static + scrolling snapshots): the
faithful ``idx0`` (render_frame over a ZEROED base -> indices) is a pure SCREEN-SPACE windowing of the visible
12x20 tile grid -- tile ``(row,col)`` at screen ``(col*16, row*16 - fine_scroll)``, animated tiles
(``flag_tbl[tile]!=0``) remapped through ``anim_xlat`` to the current frame, and each tile equals its
de-planarized 16x16 cache slot (transparency is already encoded as colour index 0, so ``idx!=0`` is the tile
coverage). The ring-buffer / ``scroll_copy`` / ``col_ring`` / ``row_ring`` geometry all CANCELS in the net
screen mapping. The HUD strip (rows 176..200) is the recovered status bar, rendered via the recovered
``draw_status_bar``/``draw_hud`` and cached on ``hud_state``.

Palette-independent: this returns colour INDICES only (the L1 tile-texture cache survives palette fades); the
caller (``extract``) colourises per frame. Anything unsupported (an animated tile remapped to a non-opaque
type, an out-of-range fine scroll, missing assets) raises :class:`NativeBackgroundUnsupported` so the caller
falls back to the faithful render explicitly (never a silent approximation)."""
from __future__ import annotations

import numpy as np

from pre2.recovered.frame_renderer import VISIBLE_COLS, VISIBLE_ROWS
from pre2.recovered.hud import draw_hud, draw_status_bar
from pre2.recovered.render_frame import ASSET_LO
from pre2.recovered.renderer import CACHE_BASE, SLOT_BYTES

VIEWPORT_H = 176                 # gameplay viewport rows (the tiles); rows 176..200 are the HUD strip
TILE = 16
GRID_H = VISIBLE_ROWS * TILE     # 192 -- the windowed grid height (>=176 + max fine scroll)
_TILE_CACHE_OFF = CACHE_BASE - ASSET_LO   # tile-graphic cache base within RendererState.asset_planes (==0)
_TILE_REGION = 0x2000            # tile-graphic cache span per plane (0x5E80..0x7E80) -- the cache version key
_ID_PAL = [(i, 0, 0) for i in range(256)]   # identity de-index -> raw EGA index in R


class NativeBackgroundUnsupported(Exception):
    """The native path cannot reproduce this frame's background -> the caller must use the faithful render."""


def _decode_tile(asset_planes, gid: int) -> np.ndarray:
    """De-planarize tile-graphic ``gid``'s 16x16 cache slot to palette-independent colour indices (uint8). Over
    a zeroed base this is exactly the tile's faithful contribution (transparency already index 0)."""
    off = _TILE_CACHE_OFF + gid * SLOT_BYTES
    tex = np.zeros((TILE, TILE), dtype=np.uint8)
    for p in range(4):
        slot = np.frombuffer(asset_planes[p][off:off + SLOT_BYTES], dtype=np.uint8).reshape(TILE, 2)
        tex |= np.unpackbits(slot, axis=1) << p          # MSB-first, exactly as render_planar
    return tex


class TileTextureCache:
    """Palette-independent per-tile-graphic 16x16 index textures (L1), reused across source frames. Keyed by
    ``(gid, asset_version)`` so a level change (new tile graphics) naturally re-decodes. Bounded + diagnostics."""

    def __init__(self, *, max_entries: int = 8192):
        self._tex: dict = {}
        self.max_entries = max_entries
        self.stats = dict(hits=0, misses=0, evictions=0, entries=0,
                          colorize_s=0.0, native_s=0.0, fallbacks=0, hud_hits=0, hud_misses=0)

    @staticmethod
    def asset_version(asset_planes) -> int:
        """Cheap fingerprint of the tile-graphic cache region (changes on level load)."""
        return hash(tuple(bytes(p[:_TILE_REGION]) for p in asset_planes))

    def get(self, gid: int, version: int, asset_planes) -> np.ndarray:
        key = (gid, version)
        t = self._tex.get(key)
        if t is not None:
            self.stats["hits"] += 1
            return t
        self.stats["misses"] += 1
        if len(self._tex) >= self.max_entries:
            self.stats["evictions"] += len(self._tex)
            self._tex.clear()
        t = _decode_tile(asset_planes, gid)
        self._tex[key] = t
        self.stats["entries"] = len(self._tex)
        return t

    def hit_rate(self) -> float:
        n = self.stats["hits"] + self.stats["misses"]
        return self.stats["hits"] / n if n else 0.0


class _HudCache:
    """The recovered HUD strip (rows 176..200) cached on (hud_state, hud_chrome); changes only on score/lives/
    energy change. Palette-independent indices."""

    def __init__(self):
        self._cache: dict = {}

    def strip(self, rs, stats) -> np.ndarray:
        if rs.hud_chrome is None:                        # no HUD chrome -> faithful leaves the strip at 0
            return np.zeros((200 - VIEWPORT_H, 320), dtype=np.uint8)
        key = (repr(rs.hud_state), id(rs.hud_chrome))
        hit = self._cache.get(key)
        if hit is not None:
            stats["hud_hits"] += 1
            return hit
        stats["hud_misses"] += 1
        from sdl_view import render_planar_rgb_from_planes   # scripts/ (lazy: only when a HUD is present)
        planes = [bytearray(0x10000) for _ in range(4)]
        page = rs.dest_page
        draw_status_bar(planes, page, rs.hud_chrome.bar)
        if rs.hud_state is not None:
            draw_hud(planes, rs.hud_state, rs.hud_chrome.font, page)
        strip = render_planar_rgb_from_planes(planes, page, _ID_PAL)[VIEWPORT_H:200, :, 0].copy()
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[key] = strip
        return strip


def native_background_indices(rs, tile_cache: TileTextureCache, hud_cache: _HudCache) -> np.ndarray:
    """The full 200x320 background colour-index image (``idx0``), built natively from ``rs`` -- the drop-in
    replacement for ``render_frame(rebuild) -> deplanarize`` over a zeroed base. Raises
    :class:`NativeBackgroundUnsupported` for anything the native path doesn't cover (-> explicit faithful
    fallback). Palette-independent (indices); the caller colourises."""
    fine = rs.fine_scroll & 0xFF
    if fine > GRID_H - VIEWPORT_H:                        # fine scroll beyond one tile -> not steady gameplay
        raise NativeBackgroundUnsupported(f"fine_scroll {fine} out of range")
    if not rs.asset_planes or not rs.tiles:
        raise NativeBackgroundUnsupported("no tile assets")
    ver = tile_cache.asset_version(rs.asset_planes)
    grid = np.zeros((GRID_H, 320), dtype=np.uint8)
    tiles, flag_tbl, anim_xlat, blit_type = rs.tiles, rs.flag_tbl, rs.anim_xlat, rs.blit_type
    cy, cx = rs.camera_y, rs.camera_x
    for r in range(VISIBLE_ROWS):
        base_si = (cy * 0x100 + cx + r * 0x100) & 0xFFFF
        y = r * TILE
        for c in range(VISIBLE_COLS):
            tid = tiles[(base_si + c) & 0xFFFF]
            if flag_tbl[tid] != 0:                       # animated tile -> current-frame remap
                gid = anim_xlat[tid]
                if blit_type[gid] != 0:                  # the faithful anim grid only blits opaque (type 0)
                    raise NativeBackgroundUnsupported(f"animated tile {tid}->{gid} type {blit_type[gid]}")
            else:
                gid = tid
            grid[y:y + TILE, c * TILE:c * TILE + TILE] = tile_cache.get(gid, ver, rs.asset_planes)
    idx0 = np.empty((200, 320), dtype=np.uint8)
    idx0[:VIEWPORT_H] = grid[fine:fine + VIEWPORT_H]
    idx0[VIEWPORT_H:] = hud_cache.strip(rs, tile_cache.stats)
    return idx0
