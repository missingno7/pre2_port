"""VM seam for the secondary-entity update pass (:mod:`pre2.recovered.effects_update`).

Layout/translation only — no gameplay decisions. The DGROUP readers / tile reader / write-apply now live in
the single :mod:`pre2.views.memory_adapter`; re-exported here for the island's historical importers.
"""
from __future__ import annotations

from pre2.views.memory_adapter import DATA_SEG, apply_ds, readers, tile_reader

__all__ = ["DATA_SEG", "readers", "tile_reader", "apply_ds"]
