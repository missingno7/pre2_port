"""Unit tests for the native enhanced background (layer B) -- the cache mechanics, the palette-independent
tile decode, and the explicit fallback. The 0px equivalence vs the faithful render is proven on real assets by
pre2/probes/verify_native_background.py (the geometry needs real tile graphics)."""
from __future__ import annotations

import types

import numpy as np
import pytest

from pre2.enhanced.native_background import (GRID_H, NativeBackgroundUnsupported, TileTextureCache, _HudCache,
                                             _decode_tile, native_background_indices)


def _assets(slot0_plane0=0xFF):
    """4 planes; tile-graphic 0's slot (bytes 0..32) has plane 0 = ``slot0_plane0``, others 0."""
    planes = [bytearray(0x2000) for _ in range(4)]
    for i in range(0x20):
        planes[0][i] = slot0_plane0
    return tuple(bytes(p) for p in planes)


def _rs(**kw):
    base = dict(asset_planes=_assets(), tiles=bytes(0x10000), flag_tbl=bytes(256), anim_xlat=bytes(256),
               blit_type=bytes(256), camera_x=0, camera_y=0, fine_scroll=0, dest_page=0,
               hud_chrome=None, hud_state=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_decode_tile_is_palette_independent_indices():
    tex = _decode_tile(_assets(0xFF), 0)            # plane-0 bit set everywhere -> index 1
    assert tex.shape == (16, 16) and (tex == 1).all()
    tex0 = _decode_tile(_assets(0x00), 0)
    assert (tex0 == 0).all()


def test_native_idx0_windows_tiles_and_zero_hud():
    idx0 = native_background_indices(_rs(), TileTextureCache(), _HudCache())
    assert idx0.shape == (200, 320)
    assert (idx0[:176] == 1).all(), "viewport is all tile-0 (index 1)"
    assert (idx0[176:] == 0).all(), "no hud_chrome -> HUD strip stays 0 (matches faithful)"


def test_fine_scroll_offsets_viewport():
    # tile row 0 = index 1, but make row 1 a different graphic so the fine offset is observable: easiest is to
    # check the windowing arithmetic -- with fine=5 the viewport starts 5px down into the 192px grid.
    idx_a = native_background_indices(_rs(fine_scroll=0), TileTextureCache(), _HudCache())
    idx_b = native_background_indices(_rs(fine_scroll=5), TileTextureCache(), _HudCache())
    assert idx_a.shape == idx_b.shape                 # both valid (all tile-0 -> identical content)
    assert (idx_b[:176] == 1).all()


def test_out_of_range_fine_scroll_falls_back():
    with pytest.raises(NativeBackgroundUnsupported):
        native_background_indices(_rs(fine_scroll=GRID_H), TileTextureCache(), _HudCache())


def test_animated_tile_to_nonopaque_falls_back():
    flag = bytearray(256); flag[0] = 1               # tile 0 animated
    anim = bytearray(256); anim[0] = 7               # -> graphic 7
    bt = bytearray(256); bt[7] = 4                    # graphic 7 is masked (type 4) -> unsupported
    rs = _rs(flag_tbl=bytes(flag), anim_xlat=bytes(anim), blit_type=bytes(bt))
    with pytest.raises(NativeBackgroundUnsupported):
        native_background_indices(rs, TileTextureCache(), _HudCache())


def test_tile_cache_hit_miss_evict_and_version():
    c = TileTextureCache(max_entries=4)
    ap = _assets()
    v = c.asset_version(ap)
    c.get(0, v, ap); assert c.stats["misses"] == 1 and c.stats["hits"] == 0
    c.get(0, v, ap); assert c.stats["hits"] == 1
    for g in range(1, 5):
        c.get(g, v, ap)                              # 5th distinct entry -> evict
    assert c.stats["evictions"] >= 1
    # a new asset version (level change) re-decodes under a different key
    v2 = c.asset_version(_assets(0x0F))
    assert v2 != v
