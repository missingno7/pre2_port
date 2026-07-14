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
PLAYER_ANIM_HEIGHT = 0x7191  # player anim frame -> vertical extent (byte-indexed; reuses the sprite-geom region)
SPAWN_OFFSET_TABLE = 0x5CBD   # spawn ring -> signed X offset (word), indexed by the ring's absolute cursor value
ANIM_FRAME_TABLE = 0xA86F     # per-entity anim-frame descriptor table (section-marked scan)
ANIM_SECTION_MARKER = 0x7D01
SPEED_CURVE = 0x78C6          # distance-from-target -> vertical scroll speed [camera_scroll _v_speed]
# tile id -> a THREE-WAY union byte the player collision reads under three different masks depending on
# which pass is running: & 0xF = the ceiling-handler index (cs:[0x7DA9], player_collision collision_ceiling
# [5C26]); & 0x20 = the bridge/sag-frame flag (collision_bridge_dip [5BCE/5BEB]); & 0x10 = the side-solid
# (wall) flag (collision_side_handler [6531]).
CEIL_HANDLER = 0x805E
DIRTY_KIND = 0x4DF8            # tile id -> 0 direct-redraw / >=1 whole-grid-dirty [player_collision _bridge_dirty 5C7B]


class Tables:
    """Every named read-only DGROUP table, bound to one ``read_byte`` accessor. ``t = Tables(rb)`` then
    ``t.floor_props[tile]`` / ``t.cos[angle]`` / ``t.sprite_half_w(id)``."""

    __slots__ = ("_rb", "_read_word", "floor_props", "ceil_props", "tile_props", "cos", "sin", "sprite_geom",
                 "player_anim_height", "speed_curve", "ceil_handler", "dirty_kind")

    def __init__(self, read_byte, read_word=None):
        self._rb = read_byte
        self._read_word = read_word    # optional native word reader (for word-granular asset reads); else compose
        self.speed_curve = ByteTable(read_byte, SPEED_CURVE)
        self.floor_props = ByteTable(read_byte, FLOOR_PROPS)
        self.ceil_props = ByteTable(read_byte, CEIL_PROPS)
        self.tile_props = ByteTable(read_byte, TILE_PROPS)
        self.cos = ByteTable(read_byte, COS)
        self.sin = ByteTable(read_byte, SIN)
        self.ceil_handler = ByteTable(read_byte, CEIL_HANDLER)
        self.dirty_kind = ByteTable(read_byte, DIRTY_KIND)
        self.sprite_geom = ByteTable(read_byte, SPRITE_GEOM)  # index by (id & 0x1FFF)<<1 (+1 = height)
        self.player_anim_height = ByteTable(read_byte, PLAYER_ANIM_HEIGHT)

    def sprite_half_w(self, sprite_id: int) -> int:
        """The sprite's X half-extent byte (``[0x7190 + (id&0x1FFF)<<1]``)."""
        return self._rb((SPRITE_GEOM + ((sprite_id & 0x1FFF) << 1)) & 0xFFFF)

    def sprite_half_h(self, sprite_id: int) -> int:
        """The sprite's Y half-extent byte (``[0x7191 + (id&0x1FFF)<<1]``)."""
        return self._rb((SPRITE_GEOM + 1 + ((sprite_id & 0x1FFF) << 1)) & 0xFFFF)

    def _rw(self, off: int) -> int:
        if self._read_word is not None:
            return self._read_word(off & 0xFFFF)
        return self._rb(off & 0xFFFF) | (self._rb((off + 1) & 0xFFFF) << 8)

    def spawn_x_offset(self, ring: int) -> int:
        """The spawn X-offset word at the ring's absolute cursor position (``ring`` is itself a table-relative
        cursor value, e.g. ``SPAWN_OFFSET_TABLE + k*2`` — the 16-slot ring index into this asset)."""
        return self._rw((ring - SPAWN_OFFSET_TABLE) & 0xFFFF)

    def anim_script_word(self, cursor: int) -> int:
        """The anim-script word at ``cursor`` — a read of the loaded, read-only anim/attack-script bytecode
        asset (the projectile/effect anim cursor derefs it). Names the access as asset content, not a raw
        DGROUP word."""
        return self._rw(cursor)

    def anim_frame_lookup(self, entry_id: int, entry_type: int) -> int:
        """Recover ``1030:6954..6981`` — resolve a projected entity's anim-frame descriptor pointer: scan the
        table for the ``0x7D01`` section marker whose following word matches ``entry_type``, then within that
        section find the entry whose word equals ``entry_id - 0x138``."""
        target = (entry_id - 0x138) & 0xFFFF
        bx = ANIM_FRAME_TABLE
        while True:                                          # find the 0x7D01 marker for this type
            bx = (bx + 2) & 0xFFFF
            if self._rw(bx) == ANIM_SECTION_MARKER and self._rw((bx + 2) & 0xFFFF) == entry_type:
                break
        bx = (bx + 4) & 0xFFFF                                # past the marker + type word
        while self._rw(bx) != target:                        # find the matching id
            bx = (bx + 2) & 0xFFFF
        return bx
