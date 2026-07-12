"""VM seam for the secondary-entity update pass (:mod:`pre2.recovered.effects_update`).

Layout/translation only — no gameplay decisions. Reads DGROUP (0x1A0F) for the recovered tick functions and
applies their ``{offset: (value, width)}`` write contracts back onto live memory.
"""
from __future__ import annotations

DATA_SEG = 0x1A0F


def readers(mem):
    """``(rb, rw)`` byte/word readers over DGROUP (0x1A0F)."""
    base = (DATA_SEG << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        b = (base + (o & 0xFFFF)) & 0xFFFFF
        return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)

    return rb, rw


def tile_reader(mem):
    """``read_tile(off)`` over the level-map segment es=[0x2DDA] (used by the particle tile collision)."""
    es = mem.data[(((DATA_SEG << 4) + 0x2DDA) & 0xFFFFF)] | (mem.data[(((DATA_SEG << 4) + 0x2DDB) & 0xFFFFF)] << 8)
    base = (es << 4) & 0xFFFFF
    return lambda o: mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]


def apply_ds(mem, writes) -> None:
    """Apply a recovered ``{offset: (value, width)}`` DGROUP write contract (via THE contract seam)."""
    from pre2.views.dgroup_view import apply_contract
    apply_contract(mem, writes)
