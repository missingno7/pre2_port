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
           "collision_side_handler", "AIRBORNE_FLAG", "TILE_PROP_TABLE", "TILE_CEIL_SOLID_TABLE",
           "TILE_CEIL_HANDLER_TABLE", "GROUND_REMAP_TABLE", "BRIDGE_FLAG_TABLE", "DIRTY_KIND_TABLE",
           "SIDE_FLAG_TABLE", "WALL_MARKER_LIST", "WALL_MARKER_END"]

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
