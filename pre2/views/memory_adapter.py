"""THE DGROUP memory adapter — the single shipped home of the byte-image translation machinery.

This is the one place that knows the game's state is a byte image at data segment ``DATA_SEG`` and turns a
raw ``mem.data`` bytearray into the ``(rb, rw)`` readers, the level-map ``tile_reader``, and the write-contract
applier the recovered islands run over. Every shipped importer points HERE, so the offset-translation surface
is a single module rather than scattered per-island copies.

In the object-model end-state (docs/pre2/offset_quarantine_plan.md, Phase 3→4) this module is what moves
behind the detachable boundary: once the product runs on the name-keyed store, nothing shipped calls
``readers`` at all, and the byte adapter is needed only when the bridge is attached to (de)serialize an image.
"""
from __future__ import annotations

#: the game data segment (DGROUP) — the GOG build's DS. The one definition; import it, never re-declare.
DATA_SEG = 0x1A0F


def readers(mem):
    """``(rb, rw)`` byte/word readers over DGROUP (``DATA_SEG``). ``mem`` exposes a ``.data`` bytearray."""
    base = (DATA_SEG << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        b = (base + (o & 0xFFFF)) & 0xFFFFF
        return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)

    return rb, rw


def tile_reader(mem):
    """``read_tile(off)`` over the level-map segment ``es=[0x2DDA]`` (the particle/entity tile lookups)."""
    es = mem.data[(((DATA_SEG << 4) + 0x2DDA) & 0xFFFFF)] | (mem.data[(((DATA_SEG << 4) + 0x2DDB) & 0xFFFFF)] << 8)
    base = (es << 4) & 0xFFFFF
    return lambda o: mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]


def apply_ds(mem, writes) -> None:
    """Apply a recovered ``{offset: (value, width)}`` DGROUP write contract (via THE contract seam)."""
    from pre2.views.dgroup_view import apply_contract
    apply_contract(mem, writes)
