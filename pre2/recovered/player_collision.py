"""Prehistorik 2 player ground/tile collision — recovered native logic (pure).

The collision routine (`1030:5A96`, called from the player update at `5A41` after the Y integrate) resolves the
player against the tile map: it computes the player's tile cell, dispatches a per-tile-type handler
(`cs:[0x7D9B]`), and on contact snaps Y to the tile boundary, zeroes Yvel, and sets the airborne/state flags.
See docs/pre2/player_collision_island.md for the full boundary map.

This module recovers the island bottom-up, each leaf proven byte-exact in shadow before any live replacement.
Started with the three self-contained leaves (slope offset, fall flag, horizontal block).
"""
from __future__ import annotations

__all__ = ["collision_slope_offset", "collision_fall", "collision_hblock", "AIRBORNE_FLAG"]

AIRBORNE_FLAG = 0x6BF3   # [asm 6401] the "no ground under the player" flag (bit0 set when airborne)


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
    if (al & 0x30) == 0:                                  # [661A-661C] not a slope
        return prop & 0xFFFF
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
