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
           "AIRBORNE_FLAG", "TILE_PROP_TABLE"]

AIRBORNE_FLAG = 0x6BF3   # [asm 6401] the "no ground under the player" flag (bit0 set when airborne)
TILE_PROP_TABLE = 0x8E1D  # [asm 643C] tile id -> property byte (solid / slope 0x30 / dir 0x10 / height 0x0F)


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
