"""Regression guard for the level-9 door HUD-bleed.

``redraw_tiles`` re-blits a bonus-collect's restored tiles. The shared ASM blit entry 3B77 adds the off-screen
scroll-staging base ``0x3F40`` (``add di,0x3f40``) before copying into A000 — every recovered blit caller
composes its di in that staging frame. The bridge's ``screen_di`` is only the page-RELATIVE offset (ASM
8BA6-8BD2), so ``redraw_tiles`` must add ``SCROLL_BASE`` itself. Omitting it put the door's tiles at the page
top (di 0x0000…) where they bled over the HUD band instead of changing the door tile.
"""
from __future__ import annotations

import pre2.bridge.object_interaction as oi
from pre2.bridge.frame import SCROLL_BASE


def test_redraw_tiles_targets_scroll_staging(monkeypatch):
    captured = []
    monkeypatch.setattr(oi, "blit_sprite",
                        lambda planes, idx, di, typ, bg_off, mask=b"": captured.append((idx, di, typ)))
    monkeypatch.setattr(oi._spr, "plane_views", lambda mem: [bytearray(1) for _ in range(4)])
    monkeypatch.setattr(oi._frame, "read_blit_type_table", lambda mem: bytes(0x200))  # all type-0 opaque
    monkeypatch.setattr(oi._frame, "read_mask_region", lambda mem: b"\x00" * 0x400)
    monkeypatch.setattr(oi._frame, "read_bg_off", lambda mem: 0)

    map_off = 0x3025          # the level-9 door's top-left destroyed cell (row 48, col 37)
    tile_id = 0xBE
    oi.redraw_tiles(mem=None, redraws=[map_off], map_writes={map_off: (tile_id, 1)})

    assert len(captured) == 1
    idx, di, _typ = captured[0]
    assert idx == tile_id
    assert di == (oi.screen_di(map_off) + SCROLL_BASE) & 0xFFFF   # blits into the staging frame
    assert di != oi.screen_di(map_off)                            # ... NOT the bare page-relative offset (the bug)


def test_redraw_tiles_empty_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(oi, "blit_sprite", lambda *a, **k: calls.append(a))
    oi.redraw_tiles(mem=None, redraws=[], map_writes={})
    assert calls == []
