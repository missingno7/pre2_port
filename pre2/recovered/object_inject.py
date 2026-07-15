"""The secondary-entity render-injection pass (1030:6913..698B + the 0x7Dxx/0x7Exx/0x7Fxx handlers).

After the main object-update walker (684E..6913), a SECOND pass walks the variable-stride entity list at
``0x8489`` (the player + special entities — score popups, the player's projectiles, etc.; entry 0 = the player,
handler ``0x7D9B``). Each entry's ``[+1]`` handler index dispatches through ``cs:[bx+0x6AC3]``. Most handlers
are thin wrappers around the shared worker :func:`project_entity` (``0x7F26``): they PROJECT the entity into a
free slot of the MAIN object list ``0x4FD0`` as a render record, so the moving-sprite renderer (``26FA``) draws
it, then set the entity's mode byte ``[entry+4]``.

Operates on real ``pre2.game.model`` objects (``ArenaEntity``/``Actor``) — no DGROUP offsets, no byte-image
reads/writes, no write-contract dicts: each function mutates the objects it's handed directly. The two loaded,
read-only ASSET lookups (the spawn X-offset ring table and the anim-frame descriptor scan) go through
``pre2.views.tables.Tables`` — named/indexed asset access, not gameplay state (see
docs/pre2/name_frontier.md, "not a dissolve target").
"""
from __future__ import annotations

from pre2.game.ref import ArenaRef, ObjectRef
from pre2.recovered.object_update import on_screen_tile
from pre2.recovered.prng import rng_lcg

__all__ = ["find_free_object_slot", "find_free_actor", "project_entity", "second_pass_tick"]

INJECT_MODE = 0x17     # [asm 7F52] the entity mode set on a successful projection (wrappers override it)


def find_free_object_slot(read_id) -> int | None:
    """Recover ``1030:806C`` — the first free slot index of the object list ``0x4FD0`` (``[slot+4]==0xFFFF``),
    or ``None`` if all 12 are taken. ``read_id(slot)`` reads the slot's ``[+4]`` sprite-id word. Kept for the
    other shared callers (``player.py``/``player_interaction.py``/the VM checkpoint) that still allocate by
    index; :func:`find_free_actor` is the offset-free equivalent this module's own logic uses."""
    for slot in range(12):                               # [asm 8070 cx=0xC]
        if read_id(slot) == 0xFFFF:                     # [asm 8073 cmp [di+4],-1]
            return slot
    return None                                         # [asm 807F stc -> CF=1]


def find_free_actor(actors):
    """The offset-free equivalent of :func:`find_free_object_slot`: the ``(index, actor)`` of the first
    ``actors`` entry with an empty slot (``sprite == 0xFFFF``), or ``None``. Returns the INDEX alongside the
    object (not re-derived by a later value search — two empty slots can compare equal by value, so a search
    by value would risk picking the wrong one back)."""
    for i, actor in enumerate(actors):
        if actor.sprite == 0xFFFF:
            return i, actor
    return None


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _saturating_inc(v: int) -> int:
    """``add [off],1 ; sbb [off],0`` — increment a byte counter saturating at 0xFF."""
    v += 1
    return 0xFF if v > 0xFF else v


def _rng_roll(rng) -> int:
    """Advance the shared RNG one step (``call 39DF``), mutating ``rng`` in place; returns the roll."""
    a, b, c, d, ret = rng_lcg(rng.lcg_a, rng.lcg_b, rng.lcg_c, rng.lcg_d)
    rng.lcg_a, rng.lcg_b, rng.lcg_c, rng.lcg_d = a, b, c, d
    return ret


def handler_ground_snap_spawn(entity, entity_idx, actors, player, spawn_cursor, scenery, player_state, boss,
                              level, read_es, tables):
    """Recover ``1030:7D9B`` — second-pass handler idx10: a player-proximity-gated, ground-snapped
    sprite projector.

    Gated by: on level 5, the camera-shake/boss-phase state; a per-entity saturating counter (``entity.counter``)
    throttle vs ``entity.throttle``; and a player-proximity window (``entity.origin_x_cell``/``origin_y_cell``,
    ``entity.extent_x_cells``/``extent_y_cells``, in tile cells). On a pass it allocates a free actor, advances
    the spawn offset ring, and projects a sprite at ``playerX + a rotating ring offset`` snapped onto a standable
    ground surface (scans the terrain map upward for a solid-with-2-empty-above cell), then finishes the record.
    ``read_es`` reads the level-map byte (the level asset); ``tables`` is the loaded read-only asset lookup.
    Returns whether drawn.

    NAME CAVEAT: recovered byte-exact (witness demo 213332), but this second-pass projection does not map
    cleanly to a [[pre2-cyxx-reference]] routine, so it is named for its *verified mechanism* (ground-snap
    spawn), not a confirmed in-game identity."""
    if level == 5:                                        # [7D9B] level-5 special gates
        if player_state.camera_shake != 0:               # [7DA2] earthquake/shake active -> no draw
            return False
        if boss.boss_phase == 3:                         # [7DA9]
            return False

    entity.counter = _saturating_inc(entity.counter)      # [7DB0] saturating ++ (add ; sbb) -- unconditional
    if entity.throttle > (entity.counter >> 2):           # [7DBB] throttle
        return False

    px_cell = (_s16(player.x) >> 4) & 0xFF                # [7DC8] player X in tile cells
    if px_cell < entity.origin_x_cell:                   # [7DD0] jb
        return False
    rel_x = (px_cell - entity.origin_x_cell) & 0xFF
    if entity.extent_x_cells < rel_x:                    # [7DD4] jb
        return False
    py_cell = (_s16(player.y) >> 4) & 0xFF                # [7DD9]
    if py_cell < entity.origin_y_cell:                   # [7DDE] jb
        return False
    rel_y = (py_cell - entity.origin_y_cell) & 0xFF
    if entity.extent_y_cells < rel_y:                    # [7DE2] jb
        return False

    found = find_free_actor(actors)                       # [7DE7] no free slot -> no draw
    if found is None:
        return False
    actor_idx, actor = found

    # [7DF4] place at playerX + the next ring offset, advance the ring
    ring = spawn_cursor.spawn_offset_ring
    player_x = player.x
    new_x = (player_x + tables.spawn_x_offset(ring)) & 0xFFFF
    spawn_cursor.spawn_offset_ring = (ring + 2) & 0x0F
    xvel = 0 if _s16(player_x) >= _s16(new_x) else 0xFFFF  # [7E0C] dx=0 / not dx (sign toward the player)

    # [7DEC-7E15] these writes are BEFORE the terrain scan, so they land UNCONDITIONALLY -- even when the scan
    # below finds no standable surface and the entity is NOT drawn, the hits/proj-ptr/X/Xvel residue is left
    # in the (still-empty) slot. This ORDER is load-bearing: a failed projection leaves exactly this residue,
    # which the byte-exact oracle checks (regression demo_pre2_20260712_121135 tick 159 -- a freed slot's Xvel
    # = 0xFFFF that writing everything only on success never left).
    actor.hits = 0                                        # [7DEC]
    spawn_cursor.proj_slot_ptr = ObjectRef("actors", actor_idx)   # [7DF0]
    actor.x = new_x                                       # [7E0A]
    actor.xvel = xvel                                     # [7E15]

    # [7E18] scan the terrain map upward for a standable surface (solid here, 2 empty above)
    start = (((py_cell + 4) & 0xFF) << 8) | ((new_x >> 4) & 0xFF)   # bp = ((playerY>>4)+4):(newX>>4)
    limit = scenery.map_rows << 8                                    # dx = mapheight*0x100
    bp = start
    ground_row = None
    for _ in range(0x0A):                                # [7E66] ah = 0xA tries
        if bp < limit:                                   # [7E3B] bp below the map bottom?
            t0 = tables.floor_props[read_es(bp)]
            if t0 != 0:                                  # [7E3F] solid here
                t1 = tables.floor_props[read_es((bp - 0x100) & 0xFFFF)]
                if t1 == 0:                              # [7E48] empty one above
                    t2 = tables.floor_props[read_es((bp - 0x200) & 0xFFFF)]
                    if t2 == 0:                          # [7E52] empty two above -> standable
                        ground_row = (bp >> 8) & 0xFF
                        break
        bp = (bp - 0x100) & 0xFFFF                        # [7E5C] up a row
        if bp < 0x300:                                   # [7E60] ran off the top -> give up
            break

    if ground_row is None:                               # [7E95] no surface -> not drawn (the residue above remains)
        return False

    # [7E6C-7E90] a standable surface was found -> finish the record
    actor.y = (ground_row << 4) & 0xFFFF                  # [7E74] Y = surface row * 16
    actor.sprite = entity.sprite_ref                      # [7E77] sprite id
    actor.def_ptr = ArenaRef(entity_idx)                  # [7E7D] back-pointer to the source entity
    actor.state = 0                                       # [7E84]
    actor.yvel = 0                                        # [7E88]
    actor.hp = entity.aux5                                # [7E90] flip byte (reused: no AI type-def for injected)
    entity.mode = 0x17                                    # [7E80]
    return True


def project_entity(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, cull=True) -> bool:
    """Recover ``1030:7F26`` — project a 2nd-pass entity into a free actor slot for rendering.

    Culls off-screen via ``on_screen_tile`` (``8022``); copies the entity's X/Y/sprite id and sets ``def_ptr``
    to an ``ArenaRef`` back-pointer to the source entity; zeroes the velocity/state fields; sets the flip byte
    from the entity's ``aux5``; latches ``spawn_cursor.proj_slot_ptr`` to the projected actor (the anim-frame
    resolve step reads it back). On success sets ``entity.mode = INJECT_MODE`` and returns ``True``; off-screen
    -> ``False``, no writes.

    ``cull=False`` enters at the ``0x7F31`` mid-routine label (skip the on-screen test) — the proximity-gated
    handler ``7D1B`` calls it that way after doing its own player-relative range check."""
    if cull and not on_screen_tile(entity.x, entity.y, cam_x, cam_y):   # [7F26-7F2F] off-screen -> CF=1
        return False
    actor.hits = 0                                         # [7F36] (cleared first)
    spawn_cursor.proj_slot_ptr = ObjectRef("actors", actor_idx)   # [7F96]-equivalent latch
    actor.x = entity.x                                    # [7F3E-7F41]
    actor.y = entity.y                                     # [7F43-7F46]
    actor.sprite = entity.sprite_ref                       # [7F49-7F4C]
    actor.def_ptr = ArenaRef(entity_idx)                   # [7F4F] back-pointer to the entity
    actor.xvel = 0                                         # [7F56]
    actor.yvel = 0                                         # [7F5B]
    actor.state = 0                                        # [7F60]
    actor.hp = entity.aux5                                 # [7F64-7F67] flip/aux byte (reused)
    entity.mode = INJECT_MODE                              # [7F52]
    return True


# --- the per-type walker handlers (cs:[bx+0x6AC3], bx = ([entry+1]<<1)&0xFF) --------------------------------
# Each mutates the entity/actor objects it's handed directly and returns whether drawn. Most are thin wrappers
# around :func:`project_entity` that override the entity mode byte afterward.


def handler_project_mode(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, mode) -> bool:
    """idx1/2/3/5-8 (``7F26``/``7EE2``/``7ED8``/``7EB5``) — project + set the entity mode byte. ``mode`` is the
    only difference: 0x17 (idx1 worker), 5 (idx2 ``7EE2`` inline / idx5-8 ``7EB5``), 0x37 (idx3 ``7ED8``).
    (``7EE2`` inlines the projection writing mode 5 directly; net memory state is identical to project+override.)"""
    if not project_entity(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y):
        return False
    entity.mode = mode
    return True


def handler_7ebf(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y) -> bool:
    """idx4 (``7EBF``) — project, mode 5, and clear the entity's ``unk_f``/``unk_10`` bytes."""
    if not project_entity(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y):
        return False
    entity.mode = 5                # [7EC4]
    entity.unk_f = 0                # [7EC8/7ECF]
    entity.unk_10 = 0                # [7ED3]
    return True


def handler_7e97(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, level) -> bool:
    """idx9 (``7E97``) — clear ``unk_11`` unconditionally; on a draw set the mode to ``(old mode | 5)``,
    OR-ing bit7 on level 6."""
    entity.unk_11 = 0                # [7E97] cleared unconditionally
    old_mode = entity.mode           # [7E9B] saved before project
    if not project_entity(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y):
        return False
    m = old_mode | 5                # [7EA5]
    if level == 6:                  # [7EA7] level 6 -> bit7
        m |= 0x80
    entity.mode = m & 0xFF
    return True


def handler_7d6e(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, rng) -> bool:
    """idx11 (``7D6E``) — saturating counter throttle; on a draw set mode 0x37 and jitter the projected Y down
    by ``rng_lcg() & 0x3F`` (advancing the shared generator)."""
    entity.counter = _saturating_inc(entity.counter)
    if entity.throttle > (entity.counter >> 2):   # [7D7D]
        return False
    if not project_entity(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y):
        return False
    entity.mode = 0x37                          # [7D87]
    ret = _rng_roll(rng)                        # [7D8B call 39DF]
    actor.y = (actor.y - (ret & 0x3F)) & 0xFFFF  # [7D8E/7D91] Y -= rng&0x3f
    return True


def handler_7d1b(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, player) -> bool:
    """idx12 (``7D1B``) — player-proximity gate (player below the entity; |dx|<0x280; if |dx|<0x140 also a
    0xB4<dy<0x168 Y-band) + saturating-counter throttle; then a no-cull projection (``7F31``), mode 0x8F,
    and reset the counter."""
    if _s16(player.y) <= _s16(entity.y):        # [7D1E/7D22] player not below entity (word view: world Y)
        return False
    dxv = _s16(entity.x) - _s16(player.x)       # [7D24/7D27] (word view: world X)
    if dxv < 0:
        dxv = -dxv                               # [7D2D]
    if dxv >= 0x280:                            # [7D2F/7D32]
        return False
    if dxv < 0x140:                             # [7D34] near -> apply the Y-band
        dyv = _s16(player.y) - _s16(entity.y)   # [7D39/7D3C]
        if dyv >= 0x168 or dyv <= 0xB4:          # [7D3F/7D44]
            return False
    entity.counter = _saturating_inc(entity.counter)   # [7D49]
    if entity.throttle > (entity.counter >> 2):        # [7D58]
        return False
    if not project_entity(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, cull=False):
        return False                                    # [7D5D] 7F31 (no on-screen cull)
    entity.mode = 0x8F                          # [7D62]
    entity.counter = 0                          # [7D66] counter reset on draw
    return True


def handler_7f6c(entity, entity_idx, actors, player, player_state, spawn_cursor) -> bool:
    """idx0 (``7F6C``) — a player-relative aura: if the player tile is within the entity's window
    (origin/extent packed into the SAME storage as ``x``/``y`` — byte view here, not word), project a sprite
    at ``playerX ± 0xC0`` (the side alternating via ``player_state.aura_toggle``), ``playerY - 0xB0``, mode 7.
    Own projection (does not call :func:`project_entity`)."""
    al = (_s16(player.x) >> 4) & 0xFF                     # [7F6E/7F71] playerX tile (low byte)
    if al < entity.origin_x_cell:                         # [7F76/7F78]
        return False
    al = (al - entity.origin_x_cell) & 0xFF
    if entity.extent_x_cells < al:                        # [7F7A/7F7D]
        return False
    al2 = (_s16(player.y) >> 4) & 0xFF                    # [7F7F/7F82] playerY tile (low byte)
    if al2 < entity.origin_y_cell:                        # [7F84/7F86]
        return False
    al2 = (al2 - entity.origin_y_cell) & 0xFF
    if entity.extent_y_cells < al2:                       # [7F88/7F8B]
        return False
    found = find_free_actor(actors)                        # [7F8D]
    if found is None:
        return False
    actor_idx, actor = found
    toggle = player_state.aura_toggle ^ 1                 # [7F9D] xor aura_toggle,1
    off = 0xC0 if toggle == 0 else (-0xC0 & 0xFFFF)       # [7FA2/7FA4] je keeps +0xC0 else neg
    actor.hits = 0                                        # [7F92]
    player_state.aura_toggle = toggle                     # [7F9D]
    actor.x = (player.x + off) & 0xFFFF                   # [7FA6/7FAB]
    actor.y = (player.y - 0xB0) & 0xFFFF                  # [7FAD/7FB3]
    actor.sprite = entity.sprite_ref                      # [7FB6]
    actor.def_ptr = ArenaRef(entity_idx)                  # [7FBC] back-pointer
    entity.mode = 7                                       # [7FBF]
    actor.state = 0                                       # [7FC3]
    actor.yvel = 0                                        # [7FC7]
    actor.xvel = 0                                        # [7FCC]
    actor.hp = entity.aux5                                # [7FD1] flip
    spawn_cursor.proj_slot_ptr = ObjectRef("actors", actor_idx)   # [7F96]
    return True


def dispatch_handler(idx, entity, entity_idx, actors, player, player_state, spawn_cursor, scenery, boss,
                     level, rng, read_es, tables, cam_x, cam_y) -> bool:
    """Dispatch the 2nd-pass per-type handler (cs:[bx+0x6AC3]). Returns whether drawn. Fails loud on an unknown
    index (no silent fallback)."""
    if idx == 10:
        return handler_ground_snap_spawn(entity, entity_idx, actors, player, spawn_cursor, scenery,
                                         player_state, boss, level, read_es, tables)
    if idx == 0:
        return handler_7f6c(entity, entity_idx, actors, player, player_state, spawn_cursor)
    found = find_free_actor(actors)
    if found is None:
        return False
    actor_idx, actor = found
    if idx in (1, 2, 3, 5, 6, 7, 8):
        mode = {1: INJECT_MODE, 2: 5, 3: 0x37, 5: 5, 6: 5, 7: 5, 8: 5}[idx]
        return handler_project_mode(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, mode)
    if idx == 4:
        return handler_7ebf(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y)
    if idx == 9:
        return handler_7e97(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, level)
    if idx == 11:
        return handler_7d6e(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, rng)
    if idx == 12:
        return handler_7d1b(entity, entity_idx, actor_idx, actor, spawn_cursor, cam_x, cam_y, player)
    raise ValueError(f"second-pass walker: unrecovered handler index {idx}")


def second_pass_tick(entities, actors, player, player_state, spawn_cursor, scenery, boss, difficulty_mode,
                     level, rng, read_es, tables, cam_x, cam_y):
    """Compose the SECOND per-frame pass (1030:6913..698B) — the variable-stride entity-list walk + per-type
    dispatch + anim-frame resolve.

    Walks ``entities`` (index 0 = the player): per entry, skip if its stride marks the terminator, it's empty,
    its skip-bit is set, or (off-screen-cull applies and ``difficulty_mode.mode_copy`` doesn't override it).
    Otherwise dispatch by handler index (projects the entity into a free actor + sets the mode + latches
    ``spawn_cursor.proj_slot_ptr``), and on a draw resolve the anim-frame descriptor into the projected actor's
    anim cursor via that latch."""
    for idx_i, entity in enumerate(entities):
        stride = entity.stride                             # [6916] read the record length once
        if stride >= 0x32:
            break                                          # [691B..] the terminator entry ends the walk
        skip = (entity.empty                               # [691B] sprite ref == -1
                or (entity.skip & 4)                       # [6921] skip bit
                or (difficulty_mode.mode_copy != 1 and (entity.flags1 & 0x80)))   # [6927/692E] off-screen-cull
        if skip:
            continue
        handler_idx = entity.flags1 & 0x7F                  # [6934/6939]
        drawn = dispatch_handler(handler_idx, entity, idx_i, actors, player, player_state, spawn_cursor,
                                 scenery, boss, level, rng, read_es, tables, cam_x, cam_y)
        if drawn:                                            # [6952] CF==0 -> resolve the anim frame
            desc = tables.anim_frame_lookup(entity.sprite_ref, handler_idx)   # [6954..697B]
            di = spawn_cursor.proj_slot_ptr                   # [697D] the just-projected actor (every handler
            actors[di.index].anim_ptr = desc                  # [6981] latches this on ANY successful draw)


# ============================================================================================================
# THE BYTE-IMAGE / VM-ORACLE PATH (below) — kept for the VM hybrid hook (pre2/checkpoints/object_inject.py) and
# its direct unit tests (tests/test_object_inject.py), and as the fallback for callers whose NativeGameState is
# still on the plain byte-image backend (see native/loop.py's dispatch in native_object_system_step). This is
# the SAME recovered logic as above, operating via rb/rw/offset write-contracts instead of named objects —
# the pre-conversion form, renamed with a _bytes suffix so both forms can coexist during the transition.
# Retiring this half requires the deploy's boot path to seed the object graph directly (no byte image at all)
# so EVERY caller has real objects available; tracked in docs/pre2/offset_free_release_plan.md.
# ============================================================================================================

from pre2.views.dgroup_view import (DictBackend, PlayerGlobals, PlayerView, RenderSlot, RngView, StructView,
                                    WidthContractBackend, _U8, _U16)
from pre2.views.tables import Tables as _TablesBytes

OBJ_BASE = 0x4FD0      # the main object record list (shared with the walker)
OBJ_STRIDE = 0x12
OBJ_COUNT = 12




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



# 7D9B (idx10) — second-pass player-proximity-gated, ground-snapped sprite projector
SPAWN_OFFSET_RING = 0xA341    # 16-slot ring index into the X-offset table
PROJ_SLOT_PTR = 0xA32E       # [0xA32E] = the last projected object slot


def handler_ground_snap_spawn_bytes(rb, rw, read_es, si, find_free):
    """Recover ``1030:7D9B`` — second-pass handler idx10: a player-proximity-gated, ground-snapped
    sprite projector.

    Gated by: on level 5, the `[0x6BEA]`/`[0xA326]` state gates; a per-entity saturating counter
    ``[si+7]`` throttle vs ``[si+6]``; and a player-proximity window (`[si+9]/[si+0xA]` origin,
    `[si+0xB]/[si+0xC]` extent, in tile cells). On a pass it allocates a free object slot, advances the
    offset ring, and projects a sprite at ``playerX + a rotating ring offset`` snapped onto a standable
    ground surface (scans the terrain map es=`[0x2DDA]`, table `[0x7F5E]` upward for a solid-with-2-empty-
    above cell), then writes the projection record. ``read_es`` reads the level-map byte ``es:[off]``;
    ``find_free`` allocates a slot. Returns ``(writes, drawn)`` — ``writes`` the DS ``{offset: value}``
    byte/word contract (the counter is updated whenever the level gate passes), ``drawn`` the ASM CF==0
    (projected).

    NAME CAVEAT: recovered byte-exact (witness demo 213332), but this second-pass projection does not map
    cleanly to a [[pre2-cyxx-reference]] routine, so it is named for its *verified mechanism* (ground-snap
    spawn), not a confirmed in-game identity."""
    out: dict = {}

    be = DictBackend(rb, rw)
    g = PlayerGlobals(be)                                  # read-only named access
    pv = PlayerView(be)
    rec = LiveEntityRecordBytes(be, si)
    if g.level == 5:                                      # [7D9B] level-5 special gates
        if g.camera_shake != 0:                          # [7DA2] earthquake/shake active -> no draw
            return out, False
        if g.boss_phase == 3:                            # [7DA9]
            return out, False

    counter = rec.counter + 1                             # [7DB0] saturating ++ (add ; sbb)
    if counter > 0xFF:
        counter = 0xFF
    out[(si + 7) & 0xFFFF] = (counter, 1)
    if rec.throttle > (counter >> 2):                     # [7DBB] throttle
        return out, False

    px_cell = (_s16(pv.x) >> 4) & 0xFF                    # [7DC8] player X in tile cells
    if px_cell < rec.origin_x_cell:                       # [7DD0] jb
        return out, False
    rel_x = (px_cell - rec.origin_x_cell) & 0xFF
    if rec.extent_x_cells < rel_x:                        # [7DD4] jb
        return out, False
    py_cell = (_s16(pv.y) >> 4) & 0xFF                    # [7DD9]
    if py_cell < rec.origin_y_cell:                       # [7DDE] jb (dh = [si+0xA])
        return out, False
    rel_y = (py_cell - rec.origin_y_cell) & 0xFF
    if rec.extent_y_cells < rel_y:                        # [7DE2] jb
        return out, False

    slot = find_free()                                    # [7DE7] no free slot -> no draw
    if slot is None:
        return out, False
    base = OBJ_BASE + slot * OBJ_STRIDE

    # [7DF4] place at playerX + the next ring offset, advance the ring
    ring = g.spawn_offset_ring
    player_x = pv.x
    new_x = (player_x + _TablesBytes(rb).spawn_x_offset(ring)) & 0xFFFF
    out[SPAWN_OFFSET_RING] = ((ring + 2) & 0x0F, 2)
    xvel = 0 if _s16(player_x) >= _s16(new_x) else 0xFFFF  # [7E0C] dx=0 / not dx (sign toward the player)

    # [7DEC-7E15] these slot writes are BEFORE the terrain scan, so they land UNCONDITIONALLY — even when the
    # scan below finds no standable surface and the entity is NOT drawn, the [+0x10]/slot-ptr/X/Xvel residue is
    # left in the (still-empty) slot. The ASM's write ORDER is load-bearing: a failed projection leaves exactly
    # this residue, which the byte-exact oracle checks (regression demo_pre2_20260712_121135 tick 159 — a freed
    # slot's [+8] Xvel = 0xFFFF that the old code, writing everything only on success, never left).
    out[base + 0x10] = (0, 1)                             # [7DEC]
    out[PROJ_SLOT_PTR] = (base & 0xFFFF, 2)               # [7DF0] [0xA32E] = the projected slot
    out[base + 0x00] = (new_x, 2)                         # [7E0A] X
    out[base + 0x08] = (xvel, 2)                          # [7E15] Xvel sign

    # [7E18] scan the terrain map upward for a standable surface (solid here, 2 empty above)
    start = (((py_cell + 4) & 0xFF) << 8) | ((new_x >> 4) & 0xFF)   # bp = ((playerY>>4)+4):(newX>>4)
    limit = (g.map_rows << 8)                                        # dx = mapheight*0x100
    floor_props = _TablesBytes(rb).floor_props
    bp = start
    ground_row = None
    for _ in range(0x0A):                                # [7E66] ah = 0xA tries
        if bp < limit:                                   # [7E3B] bp below the map bottom?
            t0 = floor_props[read_es(bp)]
            if t0 != 0:                                  # [7E3F] solid here
                t1 = floor_props[read_es((bp - 0x100) & 0xFFFF)]
                if t1 == 0:                              # [7E48] empty one above
                    t2 = floor_props[read_es((bp - 0x200) & 0xFFFF)]
                    if t2 == 0:                          # [7E52] empty two above -> standable
                        ground_row = (bp >> 8) & 0xFF
                        break
        bp = (bp - 0x100) & 0xFFFF                        # [7E5C] up a row
        if bp < 0x300:                                   # [7E60] ran off the top -> give up
            break

    if ground_row is None:                               # [7E95] no surface -> not drawn (the residue above remains)
        return out, False

    # [7E6C-7E90] a standable surface was found -> finish the record
    out[base + 0x02] = ((ground_row << 4) & 0xFFFF, 2)    # [7E74] Y = surface row * 16
    out[base + 0x04] = (rec.sprite_ref, 2)                # [7E77] sprite id
    out[base + 0x06] = (si & 0xFFFF, 2)                   # [7E7D] back-pointer
    out[base + 0x0E] = (0, 1)                             # [7E84] state byte
    out[base + 0x0A] = (0, 2)                             # [7E88] Yvel
    out[base + 0x0F] = (rec.aux5, 1)                      # [7E90] flip byte
    out[(si + 4) & 0xFFFF] = (0x17, 1)                    # [7E80] entity mode
    return out, True


class ProjectResultBytes:
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
        return (isinstance(o, ProjectResultBytes) and self.drawn == o.drawn and self.slot == o.slot
                and self.record == o.record and self.mode == o.mode)

    def __repr__(self):
        return f"ProjectResultBytes(drawn={self.drawn}, slot={self.slot}, record={self.record}, mode={self.mode})"


def project_entity_bytes(entry_x, entry_y, entry_sprite, entry_aux5, entry_ptr, cam_x, cam_y, find_free,
                   cull=True) -> ProjectResultBytes:
    """Recover ``1030:7F26`` — project a 2nd-pass entity into a free object-list slot for rendering.

    Culls off-screen via ``on_screen_tile`` (``8022``); allocates a free object slot via ``find_free`` (the
    recovered ``806C``); copies the entity X (``[entry+9]``), Y (``[entry+0xB]``), sprite id (``[entry+2]``) and
    a back-pointer (``[entry] -> record[+6]``) into the record, zeroes the velocity/state fields, and sets the
    record's flip byte from ``[entry+5]``. On success returns ``drawn=True`` with the record + ``mode=0x17``
    (the entity's ``[+4]`` write); off-screen or no free slot -> ``drawn=False`` (no writes).

    ``cull=False`` enters at the ``0x7F31`` mid-routine label (skip the on-screen test) — the proximity-gated
    handler ``7D1B`` calls it that way after doing its own player-relative range check."""
    if cull and not on_screen_tile(entry_x, entry_y, cam_x, cam_y):   # [7F26-7F2F] off-screen -> CF=1
        return ProjectResultBytes(False)
    slot = find_free()                                           # [7F31-7F34] no free slot -> CF=1
    if slot is None:
        return ProjectResultBytes(False)
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
    return ProjectResultBytes(True, slot=slot, record=record, mode=INJECT_MODE)   # [7F52] [entry+4]=0x17


# --- the per-type walker handlers (cs:[bx+0x6AC3], bx = ([entry+1]<<1)&0xFF) --------------------------------
# Each returns ``(writes, drawn)`` — ``writes`` the DS ``{offset: (value, width)}`` contract, ``drawn`` the ASM
# CF==0. Most are thin wrappers around :func:`project_entity_bytes` that override the entity mode byte ``[entry+4]``.
AURA_TOGGLE = 0x6BCC        # [0x6BCC] idx0 alternating ±0xC0 side flag
ENTITY_LIST = 0x8489        # the variable-stride 2nd-pass entity list (entry 0 = the player)
_BYTE_FIELDS = (0x0E, 0x0F, 0x10)


def _project_writes_bytes(pr: ProjectResultBytes, si: int):
    """Turn a drawn :class:`ProjectResultBytes` into the absolute DS write contract: the record fields into the
    allocated slot (``base = 0x4FD0 + slot*0x12``), ``[0xA32E]=base`` and the entity mode ``[si+4]=pr.mode``
    (``0x17``; wrappers override it). Returns ``(writes, base)``."""
    base = (OBJ_BASE + pr.slot * OBJ_STRIDE) & 0xFFFF
    w = {(base + f) & 0xFFFF: (v, 1 if f in _BYTE_FIELDS else 2) for f, v in pr.record.items()}
    w[PROJ_SLOT_PTR] = (base, 2)
    w[(si + 4) & 0xFFFF] = (pr.mode, 1)
    return w, base


def _entry_project_bytes(rb, rw, si, cam_x, cam_y, find_free, cull=True) -> ProjectResultBytes:
    """Call :func:`project_entity_bytes` for the entity record at ``si`` (X=[si+9], Y=[si+0xB], sprite=[si+2],
    aux/flip=[si+5], back-ptr=si)."""
    ent = LiveEntityRecordBytes(WidthContractBackend(rb, rw), si)     # read the source record fields by name
    return project_entity_bytes(ent.x, ent.y, ent.sprite_ref, ent.aux5, si & 0xFFFF, cam_x, cam_y, find_free, cull=cull)


def handler_project_mode_bytes(rb, rw, si, cam_x, cam_y, find_free, mode):
    """idx1/2/3/5-8 (``7F26``/``7EE2``/``7ED8``/``7EB5``) — project + set the entity mode byte. ``mode`` is the
    only difference: 0x17 (idx1 worker), 5 (idx2 ``7EE2`` inline / idx5-8 ``7EB5``), 0x37 (idx3 ``7ED8``).
    (``7EE2`` inlines the projection writing mode 5 directly; net memory state is identical to project+override.)"""
    pr = _entry_project_bytes(rb, rw, si, cam_x, cam_y, find_free)
    if not pr.drawn:
        return {}, False
    w, _ = _project_writes_bytes(pr, si)
    w[(si + 4) & 0xFFFF] = (mode & 0xFF, 1)
    return w, True


def handler_7ebf_bytes(rb, rw, si, cam_x, cam_y, find_free):
    """idx4 (``7EBF``) — project, mode 5, and clear the entity's ``[+0xF]`` and ``[+0x10]`` bytes."""
    pr = _entry_project_bytes(rb, rw, si, cam_x, cam_y, find_free)
    if not pr.drawn:
        return {}, False
    w, _ = _project_writes_bytes(pr, si)
    w[(si + 4) & 0xFFFF] = (5, 1)            # [7EC4]
    w[(si + 0xF) & 0xFFFF] = (0, 1)          # [7EC8/7ECF]
    w[(si + 0x10) & 0xFFFF] = (0, 1)         # [7ED3]
    return w, True


def handler_7e97_bytes(rb, rw, si, cam_x, cam_y, find_free):
    """idx9 (``7E97``) — clear ``[si+0x11]`` unconditionally; on a draw set the mode to ``(old [si+4] | 5)``,
    OR-ing bit7 on level 6."""
    rec = LiveEntityRecordBytes(DictBackend(rb, rw), si)
    out = {(si + 0x11) & 0xFFFF: (0, 1)}     # [7E97] cleared unconditionally
    old_mode = rec._mode                      # [7E9B] saved before project
    pr = _entry_project_bytes(rb, rw, si, cam_x, cam_y, find_free)
    if not pr.drawn:
        return out, False
    w, _ = _project_writes_bytes(pr, si)
    w.update(out)
    m = old_mode | 5                          # [7EA5]
    if PlayerGlobals(DictBackend(rb, rw)).level == 6:   # [7EA7] level 6 -> bit7
        m |= 0x80
    w[(si + 4) & 0xFFFF] = (m & 0xFF, 1)
    return w, True


def handler_7d6e_bytes(rb, rw, si, cam_x, cam_y, find_free):
    """idx11 (``7D6E``) — saturating counter ``[si+7]`` throttle vs ``[si+6]``; on a draw set mode 0x37 and
    jitter the projected Y down by ``rng_lcg() & 0x3F`` (advancing the shared generator)."""
    rec = LiveEntityRecordBytes(DictBackend(rb, rw), si)
    counter = _saturating_inc(rec.counter)
    out = {(si + 7) & 0xFFFF: (counter, 1)}
    if rec.throttle > (counter >> 2):            # [7D7D]
        return out, False
    pr = _entry_project_bytes(rb, rw, si, cam_x, cam_y, find_free)
    if not pr.drawn:
        return out, False
    w, base = _project_writes_bytes(pr, si)
    w.update(out)
    w[(si + 4) & 0xFFFF] = (0x37, 1)          # [7D87]
    ret = RngView(WidthContractBackend(rb, rw, w)).roll()                        # [7D8B call 39DF]
    w[(base + 0x02) & 0xFFFF] = ((pr.record[0x02] - (ret & 0x3F)) & 0xFFFF, 2)   # [7D8E/7D91] Y -= rng&0x3f
    return w, True


def handler_7d1b_bytes(rb, rw, si, cam_x, cam_y, find_free):
    """idx12 (``7D1B``) — player-proximity gate (player below the entity; |dx|<0x280; if |dx|<0x140 also a
    0xB4<dy<0x168 Y-band) + saturating-counter throttle; then a no-cull projection (``7F31``), mode 0x8F,
    and reset ``[si+7]``."""
    be = DictBackend(rb, rw)
    pv = PlayerView(be)
    rec = LiveEntityRecordBytes(be, si)
    if _s16(pv.y) <= _s16(rec.y):                             # [7D1E/7D22] player not below entity
        return {}, False
    dxv = _s16(rec.x) - _s16(pv.x)                            # [7D24/7D27]
    if dxv < 0:
        dxv = -dxv                                           # [7D2D]
    if dxv >= 0x280:                                         # [7D2F/7D32]
        return {}, False
    if dxv < 0x140:                                         # [7D34] near -> apply the Y-band
        dyv = _s16(pv.y) - _s16(rec.y)                        # [7D39/7D3C]
        if dyv >= 0x168 or dyv <= 0xB4:                     # [7D3F/7D44]
            return {}, False
    counter = _saturating_inc(rec.counter)                         # [7D49]
    out = {(si + 7) & 0xFFFF: (counter, 1)}
    if rec.throttle > (counter >> 2):                       # [7D58]
        return out, False
    pr = _entry_project_bytes(rb, rw, si, cam_x, cam_y, find_free, cull=False)   # [7D5D] 7F31 (no on-screen cull)
    if not pr.drawn:
        return out, False
    w, _ = _project_writes_bytes(pr, si)
    w.update(out)
    w[(si + 4) & 0xFFFF] = (0x8F, 1)                        # [7D62]
    w[(si + 7) & 0xFFFF] = (0, 1)                           # [7D66] counter reset on draw
    return w, True


def handler_7f6c_bytes(rb, rw, si, cam_x, cam_y, find_free):
    """idx0 (``7F6C``) — a player-relative aura: if the player tile is within the entity's window
    (origin [si+9]/[si+0xA], extent [si+0xB]/[si+0xC]), project a sprite at ``playerX ± 0xC0`` (the side
    alternating via the ``[0x6BCC]`` toggle), ``playerY - 0xB0``, mode 7. Own projection (does not call 7F26)."""
    be = DictBackend(rb, rw)
    pv = PlayerView(be)
    g = PlayerGlobals(be)
    rec = LiveEntityRecordBytes(be, si)
    al = (_s16(pv.x) >> 4) & 0xFF                            # [7F6E/7F71] playerX tile (low byte)
    if al < rec.origin_x_cell:                               # [7F76/7F78]
        return {}, False
    al = (al - rec.origin_x_cell) & 0xFF
    if rec.extent_x_cells < al:                              # [7F7A/7F7D]
        return {}, False
    al2 = (_s16(pv.y) >> 4) & 0xFF                           # [7F7F/7F82] playerY tile (low byte)
    if al2 < rec.origin_y_cell:                              # [7F84/7F86]
        return {}, False
    al2 = (al2 - rec.origin_y_cell) & 0xFF
    if rec.extent_y_cells < al2:                             # [7F88/7F8B]
        return {}, False
    slot = find_free()                                      # [7F8D]
    if slot is None:
        return {}, False
    toggle = g.aura_toggle ^ 1                              # [7F9D] xor [0x6bcc],1
    off = 0xC0 if toggle == 0 else (-0xC0 & 0xFFFF)         # [7FA2/7FA4] je keeps +0xC0 else neg
    base = (OBJ_BASE + slot * OBJ_STRIDE) & 0xFFFF
    w = {
        (base + 0x10) & 0xFFFF: (0, 1),                     # [7F92]
        AURA_TOGGLE: (toggle, 1),                           # [7F9D]
        (base + 0x00) & 0xFFFF: ((pv.x + off) & 0xFFFF, 2),   # [7FA6/7FAB] X = playerX ± 0xC0
        (base + 0x02) & 0xFFFF: ((pv.y - 0xB0) & 0xFFFF, 2),  # [7FAD/7FB3] Y = playerY - 0xB0
        (base + 0x04) & 0xFFFF: (rec.sprite_ref, 2),          # [7FB6] sprite id
        (base + 0x06) & 0xFFFF: (si & 0xFFFF, 2),                    # [7FBC] back-pointer
        (si + 4) & 0xFFFF: (7, 1),                                   # [7FBF] entity mode
        (base + 0x0E) & 0xFFFF: (0, 1),                              # [7FC3]
        (base + 0x0A) & 0xFFFF: (0, 2),                              # [7FC7]
        (base + 0x08) & 0xFFFF: (0, 2),                              # [7FCC]
        (base + 0x0F) & 0xFFFF: (rec.aux5, 1),                       # [7FD1] flip
        PROJ_SLOT_PTR: (base, 2),                                    # [7F96]
    }
    return w, True


# idx -> handler. idx10 (player trail) has a different signature (needs read_es) — dispatched specially.
_HANDLERS = {
    0: handler_7f6c_bytes,
    1: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, INJECT_MODE),
    2: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, 5),
    3: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, 0x37),
    4: handler_7ebf_bytes,
    5: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, 5),
    6: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, 5),
    7: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, 5),
    8: lambda rb, rw, si, cx, cy, ff: handler_project_mode_bytes(rb, rw, si, cx, cy, ff, 5),
    9: handler_7e97_bytes,
    11: handler_7d6e_bytes,
    12: handler_7d1b_bytes,
}


def dispatch_handler_bytes(idx, rb, rw, read_es, si, cam_x, cam_y, find_free):
    """Dispatch the 2nd-pass per-type handler (cs:[bx+0x6AC3]). Returns ``(writes, drawn)``. Fails loud on an
    unknown index (no silent fallback)."""
    if idx == 10:
        return handler_ground_snap_spawn_bytes(rb, rw, read_es, si, find_free)
    h = _HANDLERS.get(idx)
    if h is None:
        raise ValueError(f"second-pass walker: unrecovered handler index {idx}")
    return h(rb, rw, si, cam_x, cam_y, find_free)


ENTITY_STRIDE_END = 0x32    # [si] >= this ends the walk


class LiveEntityRecordBytes(StructView):
    """A live, NAME-keyed view of ONE 2nd-pass entity record at cursor ``si`` (``_base``), reading its header
    fields through the backend (so it reflects committed writes AND routes name-capably — the header reads are
    view accesses, not raw ``rb(si+off)``). Names the record-relative offsets so the walk speaks the entity's
    vocabulary — ``rec.sprite_ref`` / ``rec.handler_idx`` / ``rec.off_screen_cull``. The live twin of
    :class:`pre2.bridge.state_object.EntityRecord` (the snapshot form); the field offsets are the same.
    ``si`` aliases the StructView ``_base`` so the walk can advance the cursor (``rec.si += stride``)."""

    __slots__ = ()

    stride     = _U8(0)     # [+0] record length in bytes
    flags1     = _U8(1)     # [+1] bit7 = off-screen-cull, bits0-6 = handler index
    sprite_ref = _U16(2)    # [+2] word; 0xFFFF = empty (also the projected sprite id)
    _mode      = _U8(4)     # [+4] the record mode byte (bit2 = skip)
    aux5       = _U8(5)     # [+5] the flip/aux byte copied into the projected slot
    throttle   = _U8(6)     # [+6] the per-entity draw-throttle compare threshold
    counter    = _U8(7)     # [+7] the saturating per-entity draw-throttle counter
    x          = _U16(9)    # [+9] world X (projected into the object slot) -- a UNION with the packed
    #                          origin_x_cell(lo)/origin_y_cell(hi) proximity-window origin
    y          = _U16(0xB)  # [+0xB] world Y -- a UNION with the packed extent_x_cells(lo)/extent_y_cells(hi)
    #                          proximity-window extent
    origin_x_cell = _U8(9)     # width alias of x's low byte
    origin_y_cell = _U8(0xA)   # width alias of x's high byte
    extent_x_cells = _U8(0xB)  # width alias of y's low byte
    extent_y_cells = _U8(0xC)  # width alias of y's high byte
    unk_f      = _U8(0xF)   # cleared by a couple of handlers; role not yet evidenced
    unk_10     = _U8(0x10)  # cleared by a couple of handlers; role not yet evidenced
    unk_11     = _U8(0x11)  # cleared unconditionally by handler_7e97; role not evidenced

    @property
    def si(self) -> int:
        return self._base

    @si.setter
    def si(self, v: int) -> None:
        self._base = v & 0xFFFF

    @property
    def handler_idx(self) -> int:
        return self.flags1 & 0x7F

    @property
    def off_screen_cull(self) -> bool:
        return bool(self.flags1 & 0x80)

    @property
    def empty(self) -> bool:
        return self.sprite_ref == 0xFFFF

    @property
    def skip_flag(self) -> bool:                         # [+4] bit2 = skip
        return bool(self._mode & 4)


def second_pass_tick_bytes(rb, rw, apply_writes, read_es, cam_x, cam_y):
    """Compose the SECOND per-frame pass (1030:6913..698B) — the variable-stride entity-list walk + per-type
    dispatch + anim-frame resolve + stride advance.

    Walks the list at ``0x8489``: per entry, skip if stride ``[si] >= 0x32`` (end), ``[si+2]==-1`` (empty),
    ``[si+4] & 4``, or (``[0xB198]!=1`` and ``[si+1] & 0x80``). Otherwise dispatch handler index
    ``[si+1] & 0x7F`` (it projects the entity into a free object slot + sets the mode), apply its writes, and
    on a draw resolve the anim-frame descriptor (``lookup_anim_frame``) into the projected slot's ``[+0xC]``
    (``di=[0xA32E]``). Advance ``si`` by the stride. ``apply_writes(dict)`` commits a handler's
    ``{offset:(value,width)}`` so later entries' ``find_free`` sees the slots already taken; ``rb``/``rw`` read
    through those committed writes."""
    be = WidthContractBackend(rb, rw)
    b198 = PlayerGlobals(be).mode_copy
    find_free = lambda: find_free_object_slot(lambda s: RenderSlot(be, OBJ_BASE + s * OBJ_STRIDE).sprite)  # noqa: E731
    rec = LiveEntityRecordBytes(be, ENTITY_LIST)   # entry 0 = the player (name-keyed walk)
    while True:
        stride = rec.stride                                      # [6916] read the record length once
        if stride >= ENTITY_STRIDE_END:
            return rec.si                                        # si at the terminator entry (the ASM's 698B si)
        skip = (rec.empty                                        # [691B] sprite ref == -1
                or rec.skip_flag                                # [6921] [si+4] & 4
                or (b198 != 1 and rec.off_screen_cull))         # [6927/692E] off-screen-cull flag
        if not skip:
            idx = rec.handler_idx                                # [6934/6939] [si+1] & 0x7f
            writes, drawn = dispatch_handler_bytes(idx, rb, rw, read_es, rec.si, cam_x, cam_y, find_free)
            apply_writes(writes)
            if drawn:                                            # [6952] CF==0 -> resolve the anim frame
                desc = lookup_anim_frame(rw, rec.sprite_ref, idx)   # [6954..697B]
                di = PlayerGlobals(be).proj_slot_ptr              # [697D] di = [0xA32E]
                apply_writes({(di + 0x0C) & 0xFFFF: (desc, 2)})  # [6981] [di+0xC] = descriptor
        rec.si = (rec.si + stride) & 0xFFFF                      # [6984] advance by the (positive) stride
