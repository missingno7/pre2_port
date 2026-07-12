"""Named read-only DGROUP lookup tables — the game's static data, indexed by MEANING not by offset.

These are *content* the gameplay reads (tile properties, sprite geometry, trig), not mutable state. Today they
live at fixed DGROUP offsets inside the loaded image; a table object binds a base offset + a ``read_byte``
accessor and exposes ``table[index]``, so a call site reads ``t.floor_props[tile]`` instead of the offset-laden
``rb((0x7F5E + tile) & 0xFFFF)``.

The base offsets are collected HERE (the single place they live in shipped code), which is the first step of
the read-only-content migration: in the object-model end-state these become loaded-content arrays and the base
offsets move into the detachable bridge — but the ``t.<name>[index]`` call sites do not change. See
docs/pre2/offset_quarantine_plan.md (Phase 2 / §3c).
"""
from __future__ import annotations


class ByteTable:
    """A read-only byte table at a fixed DGROUP ``base``: ``table[i]`` = the byte at ``base + i``.

    ``read_byte(off)`` is any DGROUP byte reader (the island's own ``rb`` closure, a backend's ``rb``, or the
    level-map ``read_tile``) — the table never owns memory, it only names an indexing convention."""

    __slots__ = ("_rb", "base")

    def __init__(self, read_byte, base: int):
        self._rb = read_byte
        self.base = base & 0xFFFF

    def __getitem__(self, i: int) -> int:
        return self._rb((self.base + i) & 0xFFFF)


# --- the table bases (the ONE place these layout offsets live in shipped code, pending relocation) ----------
FLOOR_PROPS = 0x7F5E   # tile id -> ground property (solidity; also the 5C04 ground-handler index *2)
CEIL_PROPS  = 0x7E5E   # tile id -> ceiling property (bit0 = ceiling-solid; also the 5C92 side-handler index *2)
TILE_PROPS  = 0x8E1D   # tile id -> collision property: solid / slope (0x30) / dir (0x10) / height (0x0F)
COS         = 0x6F90   # angle -> signed cos byte (particle/bird X velocity)
SIN         = 0x7090   # angle -> signed sin byte (particle/bird Y velocity)
SPRITE_GEOM = 0x7190   # sprite (id & 0x1FFF)<<1 -> word: low byte = width (src bytes), high byte = height (rows)


class Tables:
    """Every named read-only DGROUP table, bound to one ``read_byte`` accessor. ``t = Tables(rb)`` then
    ``t.floor_props[tile]`` / ``t.cos[angle]`` / ``t.sprite_half_w(id)``."""

    __slots__ = ("_rb", "floor_props", "ceil_props", "tile_props", "cos", "sin", "sprite_geom")

    def __init__(self, read_byte):
        self._rb = read_byte
        self.floor_props = ByteTable(read_byte, FLOOR_PROPS)
        self.ceil_props = ByteTable(read_byte, CEIL_PROPS)
        self.tile_props = ByteTable(read_byte, TILE_PROPS)
        self.cos = ByteTable(read_byte, COS)
        self.sin = ByteTable(read_byte, SIN)
        self.sprite_geom = ByteTable(read_byte, SPRITE_GEOM)  # index by (id & 0x1FFF)<<1 (+1 = height)

    def sprite_half_w(self, sprite_id: int) -> int:
        """The sprite's X half-extent byte (``[0x7190 + (id&0x1FFF)<<1]``)."""
        return self._rb((SPRITE_GEOM + ((sprite_id & 0x1FFF) << 1)) & 0xFFFF)

    def sprite_half_h(self, sprite_id: int) -> int:
        """The sprite's Y half-extent byte (``[0x7191 + (id&0x1FFF)<<1]``)."""
        return self._rb((SPRITE_GEOM + 1 + ((sprite_id & 0x1FFF) << 1)) & 0xFFFF)
