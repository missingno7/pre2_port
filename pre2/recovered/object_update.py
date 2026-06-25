"""Prehistorik 2 object-update system — recovered native logic (pure).

The per-frame **object-update walker** (`1030:684E..6913`) integrates and animates the active game objects
(enemies / pickups / effects — NOT the player, which is a separate FSM). It walks 12 slots of an 18-byte
record list at `0x4FD0`, and per non-empty slot: applies velocity, advances the animation script, then
dispatches a per-type AI handler. Boundary disasm-confirmed (`pre2/probes/probe_object_tick.py`); this module
recovers the leaves bottom-up, each proven byte-exact in shadow before any live replacement.

Object record (18 bytes, stride 0x12):
    [+0]  world X (16-bit, wraps)        [+8]  X velocity (12.4 fixed; 0xFFFF = no-X-move sentinel)
    [+2]  world Y (16-bit, wraps)        [+0xA] Y velocity (12.4 fixed)
    [+4]  sprite id | flags(0x6000) | frame   [+0xC] animation-script pointer
    [+6]  type-definition pointer ([+1]=handler index, [+4]=behaviour flags)
    [+9]  aux (bit7 = H-flip)            [+0x11] life

Recovered so far:
  * :func:`apply_velocity` — the kinematics integrate (`6861..6873`). VERIFIED 770/770 exact vs ASM.
"""
from __future__ import annotations

__all__ = ["NO_X_MOVE", "VEL_SHIFT", "apply_velocity"]

NO_X_MOVE = 0xFFFF   # [asm 686C] sentinel in [si+8]: skip the X integrate this frame
VEL_SHIFT = 4        # [asm 6854 cl=4 / 6864 sar ax,cl] velocity is 12.4 fixed point (arithmetic >>4)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def apply_velocity(x: int, y: int, xvel: int, yvel: int) -> tuple[int, int]:
    """Integrate one object's position by its velocity — recovers ``1030:6861..6873``.

    ``Y += sar(yvel, 4)`` always; ``X += sar(xvel, 4)`` unless ``xvel == 0xFFFF`` (the no-X-move sentinel).
    The shift is arithmetic (signed) and positions wrap mod 0x10000 — exactly the ASM's
    ``sar ax,cl`` + ``add word ptr [si], ax``. Returns ``(new_x, new_y)``. Pure: caller owns the record.
    """
    new_y = (y + (_s16(yvel) >> VEL_SHIFT)) & 0xFFFF          # [asm 6861-6866] unconditional Y
    if xvel == NO_X_MOVE:                                     # [asm 686C-686F] X sentinel -> no move
        new_x = x & 0xFFFF
    else:
        new_x = (x + (_s16(xvel) >> VEL_SHIFT)) & 0xFFFF      # [asm 6869-6873]
    return new_x, new_y
