"""Bridge for the 6822 spawner island (70D7 camera engine + 6ADD mode-9 boss engine).

Presents the live VM DGROUP (0x1A0F) as the recovered functions' ``(rb, rw, read_tile)`` accessors and applies
their ``{offset: (value, width)}`` write contracts in place. Pure layout/translation — the DGROUP byte/word
readers and the level-map tile reader are shared with the effects-update island.
"""
from __future__ import annotations

from pre2.views.effects_update import apply_ds, readers, tile_reader

__all__ = ["readers", "tile_reader", "apply_ds"]
