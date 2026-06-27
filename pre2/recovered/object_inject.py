"""The secondary-entity render-injection pass (1030:6913..698B + the 0x7Dxx/0x7Exx/0x7Fxx handlers).

After the main object-update walker (684E..6913), a SECOND pass walks the variable-stride entity list at
``0x8489`` (the player + special entities — score popups, the player's projectiles, etc.; entry 0 = the player,
handler ``0x7D9B``). Each entry's ``[+1]`` handler index dispatches through ``cs:[bx+0x6AC3]``. Most handlers
are thin wrappers around the shared worker :func:`project_entity` (``0x7F26``): they PROJECT the entity into a
free slot of the MAIN object list ``0x4FD0`` as a render record, so the moving-sprite renderer (``26FA``) draws
it, then set the entity's mode byte ``[entry+4]``.

This module recovers the projection keystone bottom-up; the per-type wrappers + the player FSM (``0x7D9B``)
build on it. Each block is annotated with its ``[asm <offset>]`` origin and proven byte-exact in shadow.
"""
from __future__ import annotations

from pre2.recovered.object_update import on_screen_tile

__all__ = ["OBJ_BASE", "OBJ_STRIDE", "OBJ_COUNT", "find_free_object_slot", "ProjectResult", "project_entity"]

OBJ_BASE = 0x4FD0      # the main object record list (shared with the walker)
OBJ_STRIDE = 0x12
OBJ_COUNT = 12
INJECT_MODE = 0x17     # [asm 7F52] the entity mode set on a successful projection (wrappers override it)


def find_free_object_slot(read_id) -> int | None:
    """Recover ``1030:806C`` — the first free slot of the object list ``0x4FD0`` (``[slot+4]==0xFFFF``), or
    ``None`` if all 12 are taken. ``read_id(slot)`` reads the slot's ``[+4]`` sprite-id word."""
    for slot in range(OBJ_COUNT):                       # [asm 8070 cx=0xC]
        if read_id(slot) == 0xFFFF:                     # [asm 8073 cmp [di+4],-1]
            return slot
    return None                                         # [asm 807F stc -> CF=1]


ANIM_FRAME_TABLE = 0xA86F   # the per-entity anim-frame descriptor table
ANIM_SECTION_MARKER = 0x7D01


def lookup_anim_frame(rw, entry_id: int, entry_type: int) -> int:
    """Recover ``1030:6954..6981`` — resolve a projected entity's anim-frame descriptor pointer.

    The second-pass walker runs this inline after a successful projection (when the handler returns CF=0):
    scan the table at ``0xA86F`` for the ``0x7D01`` section marker whose following word matches the entity's
    ``type`` (``[entry+1] & 0x7F``), then within that section find the entry whose word equals the entity id
    (``[entry+2] - 0x138``). Returns the descriptor pointer the walker stores into the projected object slot's
    ``[+0xC]`` (``di=[0xA32E]``). ``rw(off)`` reads a DS word."""
    target = (entry_id - 0x138) & 0xFFFF
    bx = ANIM_FRAME_TABLE
    while True:                                          # [asm 6965] find the 0x7D01 marker for this type
        bx = (bx + 2) & 0xFFFF
        if rw(bx) == ANIM_SECTION_MARKER and rw((bx + 2) & 0xFFFF) == entry_type:
            break
    bx = (bx + 4) & 0xFFFF                                # [asm 6972] past the marker + type word
    while rw(bx) != target:                              # [asm 6975] find the matching id
        bx = (bx + 2) & 0xFFFF
    return bx


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# 7D9B (idx10) — the player after-image / enemy-phase-change effect projector
PLAYER_X = 0x4F1C
PLAYER_Y = 0x4F1E
TRAIL_RING = 0xA341          # 16-slot ring index into the X-offset table
TRAIL_OFFSET_TABLE = 0x5CBD  # the offset table, read as DS:[(ring - 0x5CBD) & 0xFFFF]
TERRAIN_TABLE = 0x7F5E       # tile id -> terrain solidity (for the ground-snap scan)
MAP_HEIGHT = 0x2CF5          # [0x2CF5] level-map height (rows)
PROJ_SLOT_PTR = 0xA32E       # [0xA32E] = the last projected object slot


def handler_player_trail(rb, rw, read_es, si, find_free):
    """Recover ``1030:7D9B`` — the 2nd-pass player-relative, ground-snapped effect projector (the
    enemy-phase-change / player after-image effect: entities placed at ``playerX + a rotating offset`` and
    snapped onto a standable ground surface near the player).

    Gated by: level-5 + earthquake/`[0xA326]`; a per-entity saturating counter ``[si+7]`` throttle vs
    ``[si+6]``; and a player-proximity window (`[si+9]/[si+0xA]` origin, `[si+0xB]/[si+0xC]` extent, in tile
    cells). On a pass it allocates a free object slot, advances the offset ring, scans the terrain map
    (es=`[0x2DDA]`, table `[0x7F5E]`) upward for a solid-with-2-empty-above cell, and writes the projection
    record. ``read_es`` reads the level-map byte ``es:[off]``; ``find_free`` allocates a slot. Returns
    ``(writes, drawn)`` — ``writes`` the DS ``{offset: value}`` byte/word contract (the counter is updated
    whenever the level gate passes), ``drawn`` the ASM CF==0 (projected)."""
    out: dict = {}

    if rb(0x2D8A) == 5:                                   # [7D9B] level-5 special gates
        if rb(0x6BEA) != 0:                              # [7DA2] earthquake active -> no draw
            return out, False
        if rw(0xA326) == 3:                              # [7DA9]
            return out, False

    counter = rb((si + 7) & 0xFFFF) + 1                   # [7DB0] saturating ++ (add ; sbb)
    if counter > 0xFF:
        counter = 0xFF
    out[(si + 7) & 0xFFFF] = (counter, 1)
    if rb((si + 6) & 0xFFFF) > (counter >> 2):            # [7DBB] throttle
        return out, False

    px_cell = (_s16(rw(PLAYER_X)) >> 4) & 0xFF            # [7DC8] player X in tile cells
    if px_cell < rb((si + 9) & 0xFFFF):                  # [7DD0] jb
        return out, False
    rel_x = (px_cell - rb((si + 9) & 0xFFFF)) & 0xFF
    if rb((si + 0xB) & 0xFFFF) < rel_x:                  # [7DD4] jb
        return out, False
    py_cell = (_s16(rw(PLAYER_Y)) >> 4) & 0xFF            # [7DD9]
    if py_cell < rb((si + 0xA) & 0xFFFF):                # [7DDE] jb (dh = [si+0xA])
        return out, False
    rel_y = (py_cell - rb((si + 0xA) & 0xFFFF)) & 0xFF
    if rb((si + 0xC) & 0xFFFF) < rel_y:                  # [7DE2] jb
        return out, False

    slot = find_free()                                    # [7DE7] no free slot -> no draw
    if slot is None:
        return out, False

    # [7DF4] place at playerX + the next ring offset, advance the ring
    ring = rw(TRAIL_RING)
    player_x = rw(PLAYER_X)
    new_x = (player_x + rw((ring - TRAIL_OFFSET_TABLE) & 0xFFFF)) & 0xFFFF
    out[TRAIL_RING] = ((ring + 2) & 0x0F, 2)
    xvel = 0 if _s16(player_x) >= _s16(new_x) else 0xFFFF  # [7E0C] dx=0 / not dx (sign toward the player)

    # [7E18] scan the terrain map upward for a standable surface (solid here, 2 empty above)
    start = (((py_cell + 4) & 0xFF) << 8) | ((new_x >> 4) & 0xFF)   # bp = ((playerY>>4)+4):(newX>>4)
    limit = (rb(MAP_HEIGHT) << 8)                                    # dx = mapheight*0x100
    bp = start
    ground_row = None
    for _ in range(0x0A):                                # [7E66] ah = 0xA tries
        if bp < limit:                                   # [7E3B] bp below the map bottom?
            t0 = rb((TERRAIN_TABLE + read_es(bp)) & 0xFFFF)
            if t0 != 0:                                  # [7E3F] solid here
                t1 = rb((TERRAIN_TABLE + read_es((bp - 0x100) & 0xFFFF)) & 0xFFFF)
                if t1 == 0:                              # [7E48] empty one above
                    t2 = rb((TERRAIN_TABLE + read_es((bp - 0x200) & 0xFFFF)) & 0xFFFF)
                    if t2 == 0:                          # [7E52] empty two above -> standable
                        ground_row = (bp >> 8) & 0xFF
                        break
        bp = (bp - 0x100) & 0xFFFF                        # [7E5C] up a row
        if bp < 0x300:                                   # [7E60] ran off the top -> give up
            break

    if ground_row is None:                               # [7E95] no surface -> no draw
        return out, False

    base = OBJ_BASE + slot * OBJ_STRIDE
    out[base + 0x10] = (0, 1)                             # [7DEC]
    out[base + 0x00] = (new_x, 2)                         # [7E0A] X
    out[base + 0x08] = (xvel, 2)                          # [7E15] Xvel sign
    out[base + 0x02] = ((ground_row << 4) & 0xFFFF, 2)    # [7E74] Y = surface row * 16
    out[base + 0x04] = (rw((si + 2) & 0xFFFF), 2)         # [7E77] sprite id
    out[base + 0x06] = (si & 0xFFFF, 2)                   # [7E7D] back-pointer
    out[base + 0x0E] = (0, 1)                             # [7E84] state byte
    out[base + 0x0A] = (0, 2)                             # [7E88] Yvel
    out[base + 0x0F] = (rb((si + 5) & 0xFFFF), 1)         # [7E90] flip byte
    out[(si + 4) & 0xFFFF] = (0x17, 1)                    # [7E80] entity mode
    out[PROJ_SLOT_PTR] = (base & 0xFFFF, 2)               # [7DF0] [0xA32E] = the projected slot
    return out, True


class ProjectResult:
    """The contract of one projection (1030:7F26): whether the entity was drawn, the render record written into
    the allocated object slot, and the entity-mode write-back. When NOT drawn (off-screen or no free slot) the
    record is ``None`` and ``mode`` is ``None`` (the ASM leaves ``[entry+4]`` untouched)."""
    __slots__ = ("drawn", "slot", "record", "mode")

    def __init__(self, drawn, slot=None, record=None, mode=None):
        self.drawn = drawn      # CF==0 (on-screen + a free slot)
        self.slot = slot        # the object-list slot index it was projected into
        self.record = record    # {field_offset: value} written into the object record
        self.mode = mode        # the [entry+4] write (INJECT_MODE on success; None when not drawn)

    def __eq__(self, o):
        return (isinstance(o, ProjectResult) and self.drawn == o.drawn and self.slot == o.slot
                and self.record == o.record and self.mode == o.mode)

    def __repr__(self):
        return f"ProjectResult(drawn={self.drawn}, slot={self.slot}, record={self.record}, mode={self.mode})"


def project_entity(entry_x, entry_y, entry_sprite, entry_aux5, entry_ptr, cam_x, cam_y, find_free) -> ProjectResult:
    """Recover ``1030:7F26`` — project a 2nd-pass entity into a free object-list slot for rendering.

    Culls off-screen via ``on_screen_tile`` (``8022``); allocates a free object slot via ``find_free`` (the
    recovered ``806C``); copies the entity X (``[entry+9]``), Y (``[entry+0xB]``), sprite id (``[entry+2]``) and
    a back-pointer (``[entry] -> record[+6]``) into the record, zeroes the velocity/state fields, and sets the
    record's flip byte from ``[entry+5]``. On success returns ``drawn=True`` with the record + ``mode=0x17``
    (the entity's ``[+4]`` write); off-screen or no free slot -> ``drawn=False`` (no writes)."""
    if not on_screen_tile(entry_x, entry_y, cam_x, cam_y):       # [7F26-7F2F] off-screen -> CF=1
        return ProjectResult(False)
    slot = find_free()                                           # [7F31-7F34] no free slot -> CF=1
    if slot is None:
        return ProjectResult(False)
    record = {                                                  # the projected object record fields
        0x00: entry_x & 0xFFFF,        # [7F3E-7F41] X
        0x02: entry_y & 0xFFFF,        # [7F43-7F46] Y
        0x04: entry_sprite & 0xFFFF,   # [7F49-7F4C] sprite id (from [entry+2])
        0x06: entry_ptr & 0xFFFF,      # [7F4F] back-pointer to the entity
        0x08: 0x0000,                  # [7F56] Xvel = 0
        0x0A: 0x0000,                  # [7F5B] Yvel = 0
        0x0E: 0x00,                    # [7F60] state = 0 (byte)
        0x0F: entry_aux5 & 0xFF,       # [7F64-7F67] flip/aux byte (from [entry+5])
        0x10: 0x00,                    # [7F36] (byte, cleared first)
    }
    return ProjectResult(True, slot=slot, record=record, mode=INJECT_MODE)   # [7F52] [entry+4]=0x17
