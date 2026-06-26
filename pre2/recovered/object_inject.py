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
