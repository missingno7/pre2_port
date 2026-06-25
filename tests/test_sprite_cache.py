"""Unit tests for the palette-independent sprite-texture cache (enhanced layer A).

Covers the cache contract the live path relies on:
  * cache miss vs cache hit -> 0px (the same indexed texture is served, colourisation is deterministic),
  * a palette fade with a cache HIT recolours correctly (L1 survives, only the colour changes),
  * the L2 RGBA cache churns by palette_version (disposable during a fade) but L1 keeps hitting,
  * the cache is bounded (entries) and counts evictions,
  * diagnostics (hit/miss/eviction/colorize) are tracked.

The edge/offscreen-clipping equivalence and the whole-frame alpha=1 parity are covered by the snapshot
probes (verify_enhanced_sprite_cache.py / verify_enhanced_parity.py)."""
from __future__ import annotations

import numpy as np

from pre2.enhanced.sprite_cache import SpriteTexture, SpriteTextureCache, palette_version


def _tex(h=3, w=4):
    idx = np.arange(h * w, dtype=np.uint8).reshape(h, w) % 16
    mask = idx != 0
    return SpriteTexture(color_indices=idx, alpha_mask=mask, off_x=1, off_y=2, mode=1)


def _pal(scale=1):
    return [(min(255, i * scale), 0, 0) for i in range(256)]


def test_miss_then_hit_serves_same_texture_and_counts():
    c = SpriteTextureCache()
    assert c.get("k") is None and c.stats["misses"] == 1 and c.stats["hits"] == 0
    t = _tex()
    c.put("k", t)
    got = c.get("k")
    assert got is t and c.stats["hits"] == 1
    assert c.stats["entries"] == 1 and c.stats["bytes"] == t.nbytes


def test_cache_hit_colorize_is_pixel_identical_across_calls():
    c = SpriteTextureCache()
    t = _tex()
    c.put("k", t)
    pal, pv = _pal(), palette_version(_pal())
    a = c.colorize("k", t, pal, pv)
    b = c.colorize("k", t, pal, pv)              # L2 hit -> same array, 0px
    assert np.array_equal(a, b)
    # colourisation is exactly palette[index] with the mask as alpha (the faithful colour application)
    assert np.array_equal(a[..., 0], np.asarray(pal, np.uint8)[t.color_indices][..., 0])
    assert np.array_equal(a[..., 3] > 0, t.alpha_mask)
    assert c.stats["rgba_hits"] == 1 and c.stats["rgba_misses"] == 1


def test_palette_fade_recolors_cached_texture_correctly():
    c = SpriteTextureCache()
    t = _tex()
    c.put("k", t)
    p1, p2 = _pal(1), _pal(8)                    # two palettes (a fade step)
    r1 = c.colorize("k", t, p1, palette_version(p1))
    r2 = c.colorize("k", t, p2, palette_version(p2))
    assert not np.array_equal(r1[..., :3], r2[..., :3]), "different palette must recolour"
    assert np.array_equal(r1[..., 3], r2[..., 3]), "alpha (coverage) is palette-independent"
    # L1 still a single entry -- the fade did NOT re-extract, only recoloured
    assert c.stats["entries"] == 1
    assert c.get("k") is t                       # L1 still hits through the fade


def test_l2_churns_by_palette_version_but_l1_survives():
    c = SpriteTextureCache(rgba_max_entries=4)
    t = _tex()
    c.put("k", t)
    for s in range(10):                          # 10 distinct palette versions (a long fade)
        p = _pal(s + 1)
        c.colorize("k", t, p, palette_version(p))
    assert c.stats["rgba_misses"] == 10          # every version is an L2 miss (churn)
    assert len(c._rgba) <= 4                      # L2 stays bounded
    assert c.get("k") is t                        # but L1 is untouched


def test_bounded_by_entries_and_counts_evictions():
    c = SpriteTextureCache(max_entries=8)
    for i in range(8):
        c.put(("k", i), _tex())
    assert c.stats["entries"] == 8 and c.stats["evictions"] == 0
    c.put(("k", 99), _tex())                     # 9th -> over cap -> wholesale evict, then insert
    assert c.stats["evictions"] == 8
    assert c.stats["entries"] == 1 and ("k", 99) in c._tex


def test_palette_version_changes_with_colors():
    base = _pal(1)
    same = _pal(1)
    other = list(_pal(1)); other[5] = (123, 45, 6)   # change a sprite colour (index < 16)
    assert palette_version(base) == palette_version(same)
    assert palette_version(base) != palette_version(other)
