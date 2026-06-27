"""Prehistorik 2 player ground/tile collision — recovered native logic (pure).

The collision routine (`1030:5A96`, called from the player update at `5A41` after the Y integrate) resolves the
player against the tile map: it computes the player's tile cell, dispatches a per-tile-type handler
(`cs:[0x7D9B]`), and on contact snaps Y to the tile boundary, zeroes Yvel, and sets the airborne/state flags.
See docs/pre2/player_collision_island.md for the full boundary map.

This module recovers the island bottom-up, each leaf proven byte-exact in shadow before any live replacement.
Started with the three self-contained leaves (slope offset, fall flag, horizontal block).
"""
from __future__ import annotations

from pre2.recovered.player import player_emit_trail

__all__ = ["collision_slope_offset", "collision_fall", "collision_hblock", "collision_land",
           "collision_ceiling", "collision_ground_handler", "collision_bridge_dip",
           "collision_side_handler", "collision_airborne", "collision", "AIRBORNE_FLAG", "TILE_PROP_TABLE",
           "TILE_CEIL_SOLID_TABLE", "TILE_CEIL_HANDLER_TABLE", "GROUND_REMAP_TABLE", "BRIDGE_FLAG_TABLE",
           "DIRTY_KIND_TABLE", "SIDE_FLAG_TABLE", "WALL_MARKER_LIST", "WALL_MARKER_END",
           "PLAYER_ANIM_HEIGHT_TABLE", "COLLISION_BYTE_FIELDS"]

AIRBORNE_FLAG = 0x6BF3   # [asm 6401] the "no ground under the player" flag (bit0 set when airborne)
TILE_PROP_TABLE = 0x8E1D  # [asm 643C] tile id -> property byte (solid / slope 0x30 / dir 0x10 / height 0x0F)
TILE_CEIL_SOLID_TABLE = 0x7E5E   # [asm 5C20] tile id -> ceiling-solid flag (bit0) for the side-nudge
TILE_CEIL_HANDLER_TABLE = 0x805E  # [asm 5C26] tile id -> ceiling-handler index (cs:[0x7DA9]): 0 noop / 1 head-bump / 2 trigger
GROUND_REMAP_TABLE = 0x7F5E       # [asm 5BA8] tile id -> ground-handler index (cs:[0x7D9B]) for the foot tile
BRIDGE_FLAG_TABLE = 0x805E        # [asm 5BCE] tile id -> bit 0x20 marks a bridge/sag frame (shares the ceiling table)
DIRTY_KIND_TABLE = 0x4DF8         # [asm 5C7B] tile id -> 0 redraw (653D) / >=1 grid-dirty flags
SIDE_FLAG_TABLE = 0x805E          # [asm 6531] tile id -> bit 0x10 marks a side-solid (wall) tile
WALL_MARKER_LIST = 0x6EA9         # [asm 64FA] 10x 8-byte wall-impact markers; slot free when word == 0x55AA
WALL_MARKER_END = 0x6F49          # [asm 6525] one past the last marker slot
PLAYER_ANIM_HEIGHT_TABLE = 0x7191  # [asm 5AAF] anim frame -> player vertical extent (scan-loop row count)

# The byte-width DS fields the collision handlers write; every other DS write is a 16-bit word. Used by the
# composition's byte-level overlay so a later read sees an earlier write at the correct width.
COLLISION_BYTE_FIELDS = frozenset({
    0x4F24, 0x4F25, 0x6BF3, 0x6BD2, 0x6BD1, 0x6BD0, 0x6BE0, 0x6BE1, 0x6BEA, 0x6BC7, 0x6BC8, 0x6BE5, 0x2DF4,
    0x6BE4, 0x27D6, 0x27D8,  # off-camera trigger (65B3)
    # wall-marker slot trailing bytes (+4/+5/+7 of each 8-byte record)
    *(s + d for s in range(WALL_MARKER_LIST, WALL_MARKER_END, 8) for d in (4, 5, 7)),
})


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def collision_slope_offset(prop: int, player_x: int) -> int:
    """Recover the slope-height offset ``1030:661A``.

    For a slope tile (property bits ``0x30`` set), the player's height within the tile depends on its X position
    across the slope: ``quot = (X & 0x0F) // 3``; on an up-slope (``prop & 0x10``) the offset is
    ``quot + (prop & 0x0F)``, otherwise ``(prop & 0x0F) - quot`` — sign-extended to a word. A non-slope tile
    returns ``prop`` unchanged (the ASM leaves ``ax`` untouched on that path). Pure."""
    al = prop & 0xFF
    if (al & 0x30) == 0:                                  # [661A-661C] not a slope -> ax = cwde(prop)
        return _s8(al) & 0xFFFF
    quot = (player_x & 0x0F) // 3                         # [6620-6628] div bl=3
    low = al & 0x0F                                       # [6630 / 6637] and bl,0xF
    if al & 0x10:                                         # [662B] slope direction
        res = (quot + low) & 0xFF                         # [6633] add al,bl
    else:
        res = (low - quot) & 0xFF                         # [663A-663C] neg al ; add bl
    return _s8(res) & 0xFFFF                              # [663E] cwde


def collision_fall(flag: int) -> int:
    """Recover the fall / no-ground response ``1030:6401`` — set the airborne flag ``[0x6BF3]`` bit0. Pure:
    returns the new ``[0x6BF3]``."""
    return (flag | 1) & 0xFF


def collision_hblock(x: int, xvel: int) -> tuple:
    """Recover the horizontal-block response ``1030:6407`` (wall hit): undo the X step and stop —
    ``[0x4F1C] -= sar(Xvel, 4)`` and ``[0x4F22] = 0``. Pure: returns ``(new_x, new_xvel)``."""
    return (x - (_s16(xvel) >> 4)) & 0xFFFF, 0           # [6408-6417]


def _air_drift_x(rw, rb, bp: int) -> int:
    """Air horizontal drift + clamp ``1030:62B1`` (``bp`` = max air X speed). When ``[0x6BDB]`` is set, add a
    facing-direction accel (``(word[0x4F25] << 4) sar [0x4F24]``) to Xvel; clamp the result to ``[-bp, +bp]``.
    Returns the new ``[0x4F22]``."""
    ax = 0                                                          # [62B5]
    if rb(0x6BDB) != 0:                                            # [62B9]
        ax = (rw(0x4F25) << 4) & 0xFFFF                            # [62C0-62C9] facing << 4
    ax = _s16(ax) >> (rb(0x4F24) & 0xFF)                           # [62CB-62CF] sar ax, [0x4F24]
    dx = _s16(rw(0x4F22)) + ax                                     # [62D1-62D5]
    bp = _s16(bp)
    if dx >= bp:                                                   # [62D7]
        return bp & 0xFFFF
    if dx <= -bp:                                                  # [62DB-62DF]
        return (-bp) & 0xFFFF
    return dx & 0xFFFF                                             # [62E1]


def _gravity_y(rw, rb, bp: int) -> int:
    """Gravity + terminal-velocity clamp ``1030:6309`` (``bp`` = terminal). Add ``0x10`` to Yvel (``4`` and a
    lighter terminal ``bp>>3`` when ``[0x6BC7]==1``); clamp up to ``bp``. Returns the new ``[0x4F2A]``."""
    dx = _s16(rw(0x4F2A))                                          # [630C]
    ax = 0x10                                                      # [6310]
    bp = _s16(bp)
    if rb(0x6BC7) == 1:                                            # [6313]
        ax = 4                                                     # [631A]
        bp = bp >> 3                                               # [631D-6321] sar bp,1 x3
    dx += ax                                                       # [6323]
    return (bp if dx >= bp else dx) & 0xFFFF                       # [6325-632B]


def collision_airborne(rw, rb) -> dict:
    """Recover the off-top / in-air physics ``1030:63B5`` (run after the worker when the player is airborne, and
    from the `5B81` off-top path). Applies air drift (`62B1`, ±0x50) + gravity (`6309`, terminal 0xC0), then sets
    the fall animation. Returns the dict of writes. Pure."""
    out: dict = {0x4F22: _air_drift_x(rw, rb, 0x50),              # [63B7-63BA]
                 0x4F2A: _gravity_y(rw, rb, 0xC0)}                 # [63BD-63C0]
    yvel = _s16(out[0x4F2A])
    if rb(0x6BC5) != 0:                                           # [63C3] (dormant momentum flag)
        al = 0x2D if yvel < 0 else 0x2E                           # [63CA-63D3]
        out[0x4F20] = (((rb(0x4F25) & 0x80) << 8) | al) & 0xFFFF  # [63F4-63FB]
        return out
    if yvel <= 0:                                                # [63D6] rising / apex -> no anim change
        return out
    out[0x6BE0] = 6                                               # [63DD]
    if rb(0x6BD0) != 0:                                          # [63E2] gated -> no anim change
        return out
    al = 0x0D if rb(0x6BD2) >= 0x0C else 0x0C                     # [63E9-63F2] fall frames
    out[0x4F20] = (((rb(0x4F25) & 0x80) << 8) | al) & 0xFFFF      # [63F4-63FB]
    return out


def _dec_floor8(v: int) -> int:
    """`sub v,1 ; adc v,0` saturating decrement of a byte (clamps at 0)."""
    v &= 0xFF
    return (v - 1) & 0xFF if v != 0 else 0


def _coll_soft_land(out: dict, new_y: int, rb) -> dict:
    """The soft-land exit `1030:64D9`: zero Yvel + set the grounded state flags."""
    out[0x4F2A] = 0                                       # [64D9]
    out[0x6BE0] = _dec_floor8(rb(0x6BE0))                 # [64DF-64E4] sat-dec
    out[0x6BD1] = 0                                       # [64E9]
    out[0x6BF3] = 2                                       # [64EE]
    out[0x6BCA] = new_y & 0xFFFF                          # [64F3-64F6]
    return out


def collision_land(rb, rw, read_es, di: int) -> dict:
    """Recover the player land-on-ground routine ``1030:641F``.

    ``rb``/``rw`` read DS; ``read_es(off)`` reads the map byte ``es:[off]`` (``es=[0x2DDA]``); ``di`` is the
    player's tile pointer (computed in the 5A96 main body). Snaps Y to the tile top, applies the slope offset
    (capped by the descent ``sar(Yvel,4)``), then resolves the landing impact — soft (zero Yvel + grounded
    flags) vs hard (landing dust, camera shake ``[0x6BEA]=8``, bounce, land anim). Returns the dict of writes
    (incl. the landing-dust trail-ring writes). Pure."""
    out: dict = {0x4F24: 0}                                          # [641F]
    if _s16(rw(0x4F2A)) < 0:                                         # [6424] rising -> airborne
        out[AIRBORNE_FLAG] = (rb(AIRBORNE_FLAG) | 1) & 0xFF          # [6401]
        return out
    out[0x6BC7] = 0                                                  # [642D]
    y = rw(0x4F1E) & 0xFFF0                                          # [6432] snap Y to tile top
    out[0x4F1E] = y
    foot = rb((read_es(di & 0xFFFF) + TILE_PROP_TABLE) & 0xFFFF)     # [6437-643C] foot tile property
    if foot != 0:                                                   # [6441]
        off = _s16(collision_slope_offset(foot, rw(0x4F1C)))        # [6445]
        cap = _s16(rw(0x4F2A)) >> 4                                  # [6448] sar Yvel,4
        if cap > 0 and off >= cap:                                  # [6454/6456]
            off = cap
        out[0x4F1E] = (y + off) & 0xFFFF                             # [6478]
    else:                                                            # [645E] below tile
        below = rb((read_es((di - 0x100) & 0xFFFF) + TILE_PROP_TABLE) & 0xFFFF)
        if below != 0:                                              # [6469]
            off = _s16(collision_slope_offset(below, rw(0x4F1C)))    # [646D]
            if off < 0x10:                                          # [6470]
                out[0x4F1E] = (y + ((off - 0x10) & 0xFFFF)) & 0xFFFF  # [6475-6478]

    new_y = out[0x4F1E]
    bd2 = rb(0x6BD2)
    if bd2 <= 4:                                                    # [647C] jbe 64D9
        return _coll_soft_land(out, new_y, rb)
    trail = player_emit_trail(rw(0x4F1C), new_y, 0, rw(0x6BBE))      # [6483] 5E18 landing dust (ungated)
    if trail is not None:
        out.update(trail[0])
        out[0x6BBE] = trail[1]
    yvel = _s16(rw(0x4F2A))
    if (_s16(new_y) - _s16(rw(0x6BCA))) < 0x20 or yvel < 0x50:       # [6486-6497]
        return _coll_soft_land(out, new_y, rb)
    out[0x6BCA] = new_y & 0xFFFF                                     # [6499]
    if bd2 >= 0x14 and yvel > 0xA0:                                  # [649F-64AC]
        out[0x6BEA] = 8                                             # [64AE] camera shake
    if bd2 <= 0x0A:                                                 # [64B3] jbe 64D9
        return _coll_soft_land(out, new_y, rb)
    if (rb(0x8166) & 1) == 0:                                       # [64BA] hard land: bounce unless [0x8166]&1
        out[0x4F2A] = 0xFFE0                                        # [64C1]
    out[0x4F20] = ((rw(0x4F20) & 0xE000) | 0x000C) & 0xFFFF          # [64C7] land anim frame 0xC
    out[0x6BD2] = 0                                                 # [64D3]
    return out


def collision_ceiling(rb, rw, read_es, di: int) -> dict:
    """Recover the player ceiling (head-bump) collision ``1030:5C16..5C76`` (part of 5B81, runs when the player
    is rising into the tile above). ``read_es(off)`` reads map byte ``es:[off]``; ``di`` is the tile-above
    pointer (the player's tile minus one row); ``rb``/``rw`` read DS.

    Reads the tile above + the player's tile, dispatches the **ceiling-tile handler** (`cs:[0x7DA9]`) indexed by
    ``0x805E[tile_above] & 0xF``: idx 0 = no-op (`0x6672`); idx 1 = head-bump (`0x6673`: zero Yvel + snap Y down
    below the ceiling). Then, if the player's tile is ceiling-solid (`0x7E5E[player_tile] & 1`) and Y>0, a
    sideways corner-slip nudge. idx 2 (`0x65AF`, a special level trigger) and the side-nudge are unwitnessed and
    fail loud. Returns the dict of writes. Pure."""
    out: dict = {}
    tile_above = read_es(di & 0xFFFF)                            # [5C18]
    player_tile = read_es((di + 0x100) & 0xFFFF)                 # [5C1B]
    solid = rb((TILE_CEIL_SOLID_TABLE + player_tile) & 0xFFFF) & 1   # [5C20-5C24] ah = 0x7E5E[player_tile]
    idx = rb((TILE_CEIL_HANDLER_TABLE + tile_above) & 0xFFFF) & 0x0F  # [5C26-5C2A] cs:[0x7DA9] index

    if idx == 1:                                                # [6673] head-bump
        if _s16(rw(0x4F2A)) != 0:                               # [6678] rising -> zero Yvel + snap below ceiling
            out[0x4F2A] = 0                                     # [667A]
            out[0x4F1E] = ((rw(0x4F1E) & 0xFFF0) + 0x10) & 0xFFFF  # [6680-6685]
        else:                                                   # [668B] Yvel==0: push-out-of-solid-ceiling, unwitnessed
            raise NotImplementedError("ceiling head-bump Yvel==0 push-out (668B) not witnessed")
    elif idx != 0:                                              # idx 2 -> 0x65AF (level trigger), unwitnessed
        raise NotImplementedError(f"ceiling handler idx {idx} (0x65AF trigger) not recovered")

    if solid and _s16(out.get(0x4F1E, rw(0x4F1E))) > 0:          # [5C38-5C42] solid + Y>0 -> corner-slip nudge
        raise NotImplementedError("ceiling side-nudge (0x7E5E-solid tile) not witnessed")
    return out


def _ground_snap_or_fall(rb, rw, read_es, di: int) -> dict:
    """Ground handler 0 ``1030:65EF`` (the most common): if at rest (Yvel==0) and a reachable solid/slope tile
    sits a row below, step the player down one row and land on it; otherwise mark airborne (`0x6401`). This is
    what keeps the caveman stuck to descending ground instead of floating off the lip of a step."""
    if _s16(rw(0x4F2A)) != 0:                                       # [65EF] still moving vertically -> fall
        return {AIRBORNE_FLAG: collision_fall(rb(AIRBORNE_FLAG))}
    al = read_es((di + 0x100) & 0xFFFF)                             # [65F6] tile one row below the foot tile
    prop = rb((TILE_PROP_TABLE + al) & 0xFFFF)                      # [65FB-65FE]
    if prop == 0:                                                   # [65FF] empty below -> fall
        return {AIRBORNE_FLAG: collision_fall(rb(AIRBORNE_FLAG))}
    off = _s16(collision_slope_offset(prop, rw(0x4F1C)))            # [6603]
    if off >= 0x10:                                                # [6606-6609] too far down -> fall
        return {AIRBORNE_FLAG: collision_fall(rb(AIRBORNE_FLAG))}
    stepped_y = (rw(0x4F1E) + 0x10) & 0xFFFF                        # [660F] drop one tile row
    rw2 = lambda o: stepped_y if o == 0x4F1E else rw(o)
    out = collision_land(rb, rw2, read_es, (di + 0x100) & 0xFFFF)   # [660B/6614 -> 0x641F] land on the row below
    out.setdefault(0x4F1E, stepped_y)
    return out


def collision_ground_handler(idx: int, rb, rw, read_es, di: int) -> dict:
    """Recover the ground tile-handler dispatch ``cs:[0x7D9B]`` (`5C04`, ``bx = 0x7F5E[tile_below]*2``). ``di`` is
    the foot-tile pointer (player tile + one row); the handlers are thin compositions over the verified
    land/fall/slope cores. Returns the dict of writes. Pure.

    idx 0 `65EF` snap-down-or-fall; 1 `6641` land; 2/3/4 `6657/6660/6669` land + slope-shift ``[0x4F24]=idx-1``;
    5 `6645` ``[0x4F24]=0`` then conditional fall(`[0x6BE1]!=0`)/land; 6 `65AF` special level trigger
    (unwitnessed, fail loud); 7 `6672` no-op."""
    if idx == 0:                                                   # [65EF]
        return _ground_snap_or_fall(rb, rw, read_es, di)
    if idx == 1:                                                   # [6641] plain land
        return collision_land(rb, rw, read_es, di)
    if idx in (2, 3, 4):                                           # [6657/6660/6669] land + slope shift
        out = collision_land(rb, rw, read_es, di)
        out[0x4F24] = idx - 1                                       # 2->1, 3->2, 4->3
        return out
    if idx == 5:                                                   # [6645] conditional land/fall
        if rb(0x6BE1) != 0:                                        # [664A] blocked -> fall
            return {0x4F24: 0, AIRBORNE_FLAG: collision_fall(rb(AIRBORNE_FLAG))}
        out = collision_land(rb, rw, read_es, di)                  # [6651 -> 0x641F]
        out[0x4F24] = 0                                            # [6645] (641F also zeroes it)
        return out
    if idx == 7:                                                   # [6672] no-op
        return {}
    raise NotImplementedError(f"ground handler idx {idx} (0x65AF trigger) not recovered")  # idx 6


def _bridge_dirty(new_tile: int, ds_w: dict, rb) -> None:
    """The bridge sag/spring grid-dirty step ``1030:5C7B`` (`bx = new_tile`). For ``[0x4DF8+tile] >= 1`` mark the
    grid dirty (``[0x2DF4]=1``, ``[0x2DE0]=0x55AA``); ``== 0`` would redraw the tile directly (`653D`), which is
    unwitnessed here and fails loud."""
    if rb((DIRTY_KIND_TABLE + new_tile) & 0xFFFF) >= 1:            # [5C7B-5C80] jb 5c8e
        ds_w[0x2DF4] = 1                                            # [5C82]
        ds_w[0x2DE0] = 0x55AA                                       # [5C87]
    else:
        raise NotImplementedError("bridge-dip 653D direct tile-redraw path not witnessed")


def collision_bridge_dip(di: int, read_es, rw, rb) -> tuple:
    """Recover the bridge/platform sag-under-weight ``1030:5BB8..5C01`` (runs when the foot tile differs from the
    one currently dipping, ``[0x6BAB] != di``). ``read_es(off)`` reads the live tile map; ``di`` is the foot-tile
    pointer. Returns ``(ds_writes, map_writes)`` where ``map_writes`` maps an es-relative tile offset to its new
    byte. Pure (no live writes).

    First spring the previously-dipping tile (`[0x6BAB]`) back up — walk its graphic id **down** one frame at a
    time while the tile is still a sag frame (`0x805E[id-1] & 0x20`), writing+dirtying each, until it clears, then
    reset `[0x6BAB]=0x55AA`. Then, if the new foot tile is a bridge frame (`0x805E[id] & 0x20`), dip it **down**
    (graphic id+1), mark it as the dipping tile (`[0x6BAB]=di`), and dirty it."""
    ds_w: dict = {}
    map_w: dict = {}
    bab = rw(0x6BAB)
    if bab != 0x55AA:                                              # [5BBB] something is currently dipping
        sdi = bab & 0xFFFF
        cur = read_es(sdi)                                         # [5BC8]
        while True:
            cur = (cur - 1) & 0xFF                                 # [5BCD] dec to the previous sag frame
            if not (rb((BRIDGE_FLAG_TABLE + cur) & 0xFFFF) & 0x20):  # [5BCE-5BD3]
                ds_w[0x6BAB] = 0x55AA                               # [5BD5] sprung fully back -> none dipping
                break
            map_w[sdi] = cur                                       # [5BDE-5BE0] es:[di] = id-1
            _bridge_dirty(cur, ds_w, rb)                           # [5BE3]
    cur = read_es(di & 0xFFFF)                                     # [5BE8] the new foot tile
    if rb((BRIDGE_FLAG_TABLE + cur) & 0xFFFF) & 0x20:             # [5BEB-5BF2] is it a bridge frame?
        ds_w[0x6BAB] = di & 0xFFFF                                 # [5BF4] now dipping
        nw = (cur + 1) & 0xFF                                      # [5BF8] inc -> next sag frame
        map_w[di & 0xFFFF] = nw                                    # [5BFB] es:[di] = id+1
        _bridge_dirty(nw, ds_w, rb)                                # [5BFE]
    return ds_w, map_w


def _wall_marker_push(rw) -> dict:
    """The wall-impact marker registration ``1030:64FA``: drop ``(X<<3, Y<<3)`` into the first free 8-byte slot
    of the list at ``0x6EA9`` (free = leading word ``0x55AA``). Returns the slot's word/byte writes, or ``{}`` if
    the list is full. NOTE: never reached in any current demo (the side-solid ``0x805E&0x10`` tile never occurs;
    walls block via ``collision_hblock``) — transcribed from the ASM at ASM_MATCHED confidence, not lockstep
    VERIFIED."""
    si = WALL_MARKER_LIST
    while si < WALL_MARKER_END:                                    # [64FD-6529]
        if rw(si) == 0x55AA:                                       # [64FD] free slot
            return {si: (rw(0x4F1C) << 3) & 0xFFFF,                # [6505-650A] X<<3 (word)
                    (si + 2) & 0xFFFF: (rw(0x4F1E) << 3) & 0xFFFF,  # [650C-6511] Y<<3 (word)
                    (si + 4) & 0xFFFF: 0,                          # [6514] byte
                    (si + 5) & 0xFFFF: 0,                          # [6518] byte
                    (si + 7) & 0xFFFF: 0}                          # [651C] byte
        si += 8                                                    # [6522]
    return {}


def collision_side_handler(idx: int, read_es, rw, rb, di: int) -> dict:
    """Recover the horizontal/body side-collision dispatch ``cs:[0x7D95]`` (`5C92`/`5CAC`, ``bx = 0x7E5E[tile]*2``)
    run for each tile cell along the player's vertical extent. ``di`` is the scanned cell pointer. Returns the dict
    of writes (DS scalars and/or `0x6EA9` wall-marker slots). Pure.

    idx 0 `652C`: if the tile is **side-solid** (`0x805E[tile] & 0x10`), register a wall-impact marker (`64FA`);
    idx 1 `6539`: wall block = `collision_hblock` (undo the X step + stop); idx 2 `65AF` trigger (unwitnessed,
    fail loud). idx 3-8 reuse the ground handlers `65EF/6641/6657/6660/6669/6645` (unwitnessed in the side scan)."""
    if idx == 0:                                                   # [652C]
        tile = read_es(di & 0xFFFF)
        if rb((SIDE_FLAG_TABLE + tile) & 0xFFFF) & 0x10:           # [6531] side-solid -> wall marker
            return _wall_marker_push(rw)                           # [6536 -> 64FA]
        return {}                                                  # [6538] not solid
    if idx == 1:                                                   # [6539] wall block
        new_x, new_xvel = collision_hblock(rw(0x4F1C), rw(0x4F22))  # [6407]
        return {0x4F1C: new_x, 0x4F22: new_xvel}
    if idx in (3, 4, 5, 6, 7, 8):                                  # ground handlers, unwitnessed in side scan
        raise NotImplementedError(f"side scan ground handler idx {idx} (0x7D95) not witnessed")
    raise NotImplementedError(f"side handler idx {idx} (0x65AF trigger) not recovered")  # idx 2


class _Overlay:
    """Byte-level read-through overlay used by ``collision`` so a read later in the routine observes a write made
    earlier (the ASM mutates memory in place; the pure handlers return write dicts). DS writes are split into
    bytes using ``COLLISION_BYTE_FIELDS`` for widths; map (es) writes are byte-keyed. ``ds``/``mp`` accumulate the
    net byte writes = the routine's write-contract."""

    def __init__(self, rb, rw, read_es):
        self._rb, self._read_es = rb, read_es
        self.ds: dict = {}
        self.mp: dict = {}

    def rb(self, a: int) -> int:
        a &= 0xFFFF
        return (self.ds[a] if a in self.ds else self._rb(a)) & 0xFF

    def rw(self, a: int) -> int:
        a &= 0xFFFF
        return self.rb(a) | (self.rb((a + 1) & 0xFFFF) << 8)

    def read_es(self, o: int) -> int:
        o &= 0xFFFF
        return (self.mp[o] if o in self.mp else self._read_es(o)) & 0xFF

    def apply_ds(self, writes: dict) -> None:
        for a, v in writes.items():
            a &= 0xFFFF
            self.ds[a] = v & 0xFF
            if a not in COLLISION_BYTE_FIELDS:
                self.ds[(a + 1) & 0xFFFF] = (v >> 8) & 0xFF

    def apply_map(self, writes: dict) -> None:
        for o, v in writes.items():
            self.mp[o & 0xFFFF] = v & 0xFF


def _offcamera_trigger(rb) -> dict:
    """The off-camera death/respawn trigger ``1030:65B3`` (called as `65AF`). If not already triggered
    (``[0x6BE4]==0``): consume a life (``[0x27D8]-=1``, reset ``[0x27D6]=0``, arm respawn ``[0x6BE4]=2``); if no
    lives remain, set the game-over flag ``[0x6BE5]=1``. Returns the dict of writes. Pure."""
    if rb(0x6BE4) != 0:                                            # [65B3] already triggered
        return {}
    if rb(0x27D8) == 0:                                           # [65BA] no lives left -> game over
        return {0x6BE5: 1}                                        # [65D0]
    return {0x27D8: (rb(0x27D8) - 1) & 0xFF, 0x27D6: 0, 0x6BE4: 2}  # [65C1-65CA]


def _collision_worker(ov: _Overlay, cell_bx: int) -> None:
    """The tile-interaction worker ``1030:5B81`` composed onto the overlay ``ov`` (``cell_bx`` = the tile one row
    above the foot). Off-top (`Y<=-1`) + foot-tile remap + bridge-dip + ground dispatch + ceiling."""
    if _s16(ov.rw(0x4F1E)) <= -1:                                  # [5B84] above the top of the level
        raise NotImplementedError("collision worker off-top (5B8B, Y<=-1) not witnessed")
    di = (cell_bx + 0x100) & 0xFFFF                                # [5B97] foot tile
    foot_tile = ov.read_es(di) if ov.rb(0x2CF5) > (di >> 8) else 0  # [5B9D-5BA6] map-bounds clamp
    idx = ov.rb((GROUND_REMAP_TABLE + foot_tile) & 0xFFFF)         # [5BA8] cs:[0x7D9B] index
    if ov.rw(0x6BAB) != di:                                        # [5BB2] not already dipping here -> bridge-dip
        bds, bmp = collision_bridge_dip(di, ov.read_es, ov.rw, ov.rb)  # [5BB8]
        ov.apply_ds(bds)
        ov.apply_map(bmp)
    ov.apply_ds(collision_ground_handler(idx, ov.rb, ov.rw, ov.read_es, di))   # [5C04]
    cbx = cell_bx - 0x100                                          # [5C09]
    if cbx >= 0 and _s16(ov.rw(0x4F2A)) <= 0:                      # [5C0D jb / 5C0F jg] in-bounds + not falling
        ov.apply_ds(collision_ceiling(ov.rb, ov.rw, ov.read_es, cbx & 0xFFFF))  # [5C16]


def _side_scan(ov: _Overlay, cell: int, conditional: bool) -> None:
    """One vertical scan-loop cell ``5C92`` (first, unconditional) / ``5CAC`` (rest, only tile types 2 & 4)."""
    tile = ov.read_es(cell & 0xFFFF)                              # [5C97 / 5CB1]
    idx = ov.rb((TILE_CEIL_SOLID_TABLE + tile) & 0xFFFF)          # [5C9A / 5CB4] remap 0x7E5E
    if conditional and idx not in (2, 4):                         # [5CB8-5CBE] 5CAC dispatch filter
        return
    ov.apply_ds(collision_side_handler(idx, ov.read_es, ov.rw, ov.rb, cell & 0xFFFF))


def collision(rb, rw, read_es) -> tuple:
    """Recover the full player ground/tile collision ``1030:5A96`` (called from the player update at `5A41` after
    the Y integrate). ``rb``/``rw`` read DS; ``read_es(off)`` reads the live tile map (``es=[0x2DDA]``). Returns
    ``(ds_writes, map_writes)`` as byte-keyed dicts (the routine's complete write-contract). Pure.

    Computes the player's tile cell from X/Y, range-checks vs the camera, runs the tile-interaction worker
    (`5B81`: bridge-dip + ground dispatch + ceiling), resolves the post-worker fall/land state, then scans the
    player's vertical extent for horizontal/body collisions (`cs:[0x7D95]`)."""
    ov = _Overlay(rb, rw, read_es)

    # --- tile cell + scan parameters [5A99-5AC4] ---
    row_m1 = ((_s16(rw(0x4F1E)) >> 4) - 1) & 0xFFFF                # (Y>>4) - 1
    cell_high = ((row_m1 & 0xFF) << 8) | ((row_m1 >> 8) & 0xFF)    # [5AA4] xchg al,ah
    cell_bx = ((_s16(rw(0x4F1C)) >> 4) + cell_high) & 0xFFFF       # [5AC5-5ACB] X>>4 + (row<<8)
    anim_idx = ((rw(0x4F20) & 0x1FFF) << 1) & 0xFFFF               # [5AA6-5AAD] anim frame *2
    dh = rb((PLAYER_ANIM_HEIGHT_TABLE + anim_idx) & 0xFFFF)        # [5AAF] player vertical extent
    xvel = _s16(rw(0x4F22))
    x_edge = 9 if xvel > 0 else (-9 if xvel < 0 else 0)            # [5AB3-5AC1] leading-edge X offset

    # --- camera range check [5ACD-5B16] ---
    if _out_of_camera_range(rb, rw):                              # writes only on the out-of-range branch
        if rb(0x2D8A) == 0x0E:                                    # [5B18]
            ov.apply_ds({0x6BE5: 0xFF})                           # [5B1F]
        else:
            ov.apply_ds(_offcamera_trigger(ov.rb))               # [5B26 -> 65B3]

    ov.apply_ds({0x6BF3: 0})                                      # [5B29] clear the airborne flag
    _collision_worker(ov, cell_bx)                               # [5B2E]

    # --- post-worker fall / land [5B31-5B54] ---
    if ov.rb(0x6BF3) == 1:                                        # [5B31] airborne after the worker
        if ov.rb(0x6BFE) == 0:                                    # [5B38]
            ov.apply_ds(collision_airborne(ov.rw, ov.rb))        # [5B44 -> 63B5]
            if _s16(ov.rw(0x4F2A)) > 0:                           # [5B47] still descending
                ov.apply_ds({0x6BD2: (ov.rb(0x6BD2) + 1) & 0xFF})  # [5B4E]
        else:                                                    # [5B3F -> 64DF] soft-land tail
            ov.apply_ds({0x6BE0: _dec_floor8(ov.rb(0x6BE0)), 0x6BD1: 0, 0x6BF3: 2,
                         0x6BCA: ov.rw(0x4F1E), 0x6BD2: 0})
    else:
        ov.apply_ds({0x6BD2: 0})                                  # [5B54]

    # --- vertical side-collision scan [5B5B-5B7B] ---
    if _s16(ov.rw(0x4F1E)) > 0:                                   # [5B5B]
        bx = ((_s16(ov.rw(0x4F1C)) + x_edge) >> 4) & 0xFFFF       # [5B62-5B66]
        bx = (bx + cell_high) & 0xFFFF                            # [5B68]
        _side_scan(ov, bx, conditional=False)                    # [5B6A] first cell
        dh_left = dh
        while True:
            if bx < 0x100:                                       # [5B72-5B76] sub bx,0x100 ; jb
                break
            bx = (bx - 0x100) & 0xFFFF
            dh_left = (dh_left - 0x10) & 0xFF                     # [5B78]
            if dh_left == 0 or dh_left > 0x80:                   # [5B7B] ja (unsigned >0 after sub means no borrow)
                break
            _side_scan(ov, bx, conditional=True)                 # [5B6F] subsequent cells

    return ov.ds, ov.mp


def _out_of_camera_range(rb, rw) -> bool:
    """The camera visibility test ``1030:5ACD-5B16`` — True when the player is off the visible map (the only
    branch with side effects). Pure."""
    if abs((_s16(rw(0x4F1E)) >> 4) - _s16(rw(0x2DE6))) > 0x0B:    # [5ACD-5ADD]
        return True
    if abs((_s16(rw(0x4F1C)) >> 4) - _s16(rw(0x2DE4))) > 0x14:    # [5ADF-5AEF]
        return True
    if (rb(0x8166) & 4) and _s16(rw(0x4F1E)) < ((_s16(rw(0x2DE6)) << 4) & 0xFFFF):  # [5AF1-5B01]
        return True
    if _s16(rw(0x4F1E)) < 0:                                      # [5B03] above the top -> in range (no trigger)
        return False
    return _s16(rw(0x4F1E)) > (((rb(0x2CF5) + 1) << 4) & 0xFFFF)  # [5B0A-5B16] below the map bottom
