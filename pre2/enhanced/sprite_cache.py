"""Palette-independent sprite-texture cache (enhanced layer A).

The enhanced compositor must NOT re-run the faithful sprite paint/deplanarize every source frame. This cache
stores the expensive part once, in a PALETTE-INDEPENDENT representation, so the same texture survives palette
fades / DAC changes / truecolor fades / transition effects:

    SpriteTexture: color_indices (uint8) + alpha_mask (bool) + hotspot (off_x/off_y) + draw mode.

Each frame applies the current palette cheaply (``rgb = palette[color_indices]``). An optional second-level
RGBA cache keyed by (texture_key, palette_version) memoises the colourised result during stable gameplay; it
is DISPOSABLE (bypassed/churned during a fade) and bounded separately.

Migration role (per the recovery plan): faithful paint/deplanarize POPULATES the cache on a miss; cache hits
serve the cached indexed texture; later the population path is replaced by a native sprite decoder. Cache
hits must be pixel-identical to the faithful extraction (before palette application) -- proven by
``tests/test_sprite_cache.py`` (miss vs hit 0px) and the alpha=1 enhanced parity probe.

Cache key (set by the caller, ``pre2.enhanced.extract._texture_key``): only things that change the sprite
PIXELS -- cel identity (src segment+offset), decoded geometry (full byte-width + full rows), flip, draw mode.
NOT world/screen position and NOT off-screen clipping: the cached texture is the FULL UNCLIPPED sprite and the
compositor's ``_blit`` crops it to the screen edge, so an edge sprite still hits the cache every frame.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SpriteTexture:
    """One sprite cel as a palette-INDEPENDENT texture + its hotspot (the faithful extraction's output before
    palette application). ``off_x``/``off_y`` place the texture's top-left relative to the sprite's logical
    screen position (``screen_x``/``screen_y``); the compositor blits at ``screen + off`` and clips to edges."""
    color_indices: np.ndarray   # uint8 (H, W) -- EGA colour indices (palette applied at compose time)
    alpha_mask: np.ndarray      # bool  (H, W) -- True where the sprite is opaque
    off_x: int
    off_y: int
    mode: int                   # draw mode (object_render.MODE_NORMAL); metadata for diagnostics/future ERASE

    @property
    def nbytes(self) -> int:
        return int(self.color_indices.nbytes + self.alpha_mask.nbytes)


def palette_version(palette) -> int:
    """A cheap version id for the 16 sprite colours (4-plane EGA sprites only ever index 0..15). Changes on
    every palette/DAC/fade step, so it correctly churns the disposable RGBA L2 while the indexed L1 survives."""
    return hash(tuple(tuple(c) for c in palette[:16]))


class SpriteTextureCache:
    """Bounded, two-level sprite-texture cache with diagnostics.

    L1 (primary, palette-independent): key -> :class:`SpriteTexture`. Survives palette changes.
    L2 (optional, disposable): (key, palette_version) -> RGBA ndarray. Memoises colourisation during stable
    gameplay; bypassed-by-churn during fades. Both levels are bounded (entries + total bytes for L1)."""

    def __init__(self, *, max_entries: int = 4096, max_bytes: int = 32 << 20,
                 rgba_max_entries: int = 2048):
        self._tex: dict = {}                 # L1: key -> SpriteTexture
        self._rgba: dict = {}                # L2: (key, palette_version) -> rgba
        self._bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.rgba_max_entries = rgba_max_entries
        self.stats = dict(hits=0, misses=0, evictions=0, rgba_hits=0, rgba_misses=0,
                          colorize_s=0.0, entries=0, bytes=0, rgba_entries=0)

    # -- L1: palette-independent textures --
    def get(self, key) -> "SpriteTexture | None":
        t = self._tex.get(key)
        if t is None:
            self.stats["misses"] += 1
        else:
            self.stats["hits"] += 1
        return t

    def put(self, key, tex: SpriteTexture) -> None:
        if key in self._tex:
            return
        if len(self._tex) >= self.max_entries or self._bytes + tex.nbytes > self.max_bytes:
            self._evict_all()
        self._tex[key] = tex
        self._bytes += tex.nbytes
        self.stats["entries"] = len(self._tex)
        self.stats["bytes"] = self._bytes

    def _evict_all(self) -> None:
        # Bounded by a wholesale clear (textures are tiny; unique cels per level are few hundred, so a high cap
        # almost never trips). Eviction count tracks how many entries were discarded.
        self.stats["evictions"] += len(self._tex)
        self._tex.clear()
        self._rgba.clear()
        self._bytes = 0

    # -- L2: disposable colourised RGBA --
    def colorize(self, key, tex: SpriteTexture, palette, pversion: int) -> np.ndarray:
        """Apply ``palette`` to a cached indexed texture (memoised per palette_version). The L1 texture is
        unchanged, so a palette fade just recolours -- never re-extracts."""
        rk = (key, pversion)
        r = self._rgba.get(rk)
        if r is not None:
            self.stats["rgba_hits"] += 1
            return r
        self.stats["rgba_misses"] += 1
        t0 = time.perf_counter()
        pal = np.asarray(palette, dtype=np.uint8)
        rgba = np.zeros((*tex.color_indices.shape, 4), dtype=np.uint8)
        rgba[..., :3] = pal[tex.color_indices]
        rgba[..., 3] = np.where(tex.alpha_mask, 255, 0).astype(np.uint8)
        self.stats["colorize_s"] += time.perf_counter() - t0
        if len(self._rgba) >= self.rgba_max_entries:     # disposable -> a simple wholesale clear when full
            self._rgba.clear()
        self._rgba[rk] = rgba
        self.stats["rgba_entries"] = len(self._rgba)
        return rgba

    # -- diagnostics --
    def hit_rate(self) -> float:
        n = self.stats["hits"] + self.stats["misses"]
        return self.stats["hits"] / n if n else 0.0

    def summary(self) -> str:
        s = self.stats
        return (f"tex L1 hit={self.hit_rate()*100:.0f}% ({s['hits']}/{s['hits']+s['misses']}) "
                f"entries={s['entries']} bytes={s['bytes']>>10}K evict={s['evictions']} "
                f"| RGBA L2 hit={s['rgba_hits']}/{s['rgba_hits']+s['rgba_misses']} "
                f"colorize={s['colorize_s']*1000:.1f}ms")
