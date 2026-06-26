"""Prehistorik 2 player FSM — recovered native logic (pure).

The player update routine (`1030:~5890..5A95`, called per gameplay frame) reads the 6 input flags
(`[0x27E8..0x27ED]`), updates the player FSM state + facing, dispatches a per-state handler
(`call cs:[bx+0x7D2F]`), then runs the common kinematics: integrate Xvel/Yvel, ground/tile collision, and a
block of per-frame timer decrements. The player struct is at `0x4F1C`:

    [+0]  world X (0x4F1C)      [+6]  X velocity (0x4F22, 12.4 fixed)
    [+2]  world Y (0x4F1E)      [+8]  state-ish (0x4F24)
    [+4]  tile col (0x4F20)     [+9]  facing +1/-1 (0x4F25)
                                [+0xE] Y velocity (0x4F2A, 12.4 fixed)

This module recovers the FSM bottom-up, each leaf proven byte-exact in shadow before any live replacement.
Started with the isolated horizontal-kinematics leaf (the player counterpart of the object `apply_velocity`).
"""
from __future__ import annotations

__all__ = [
    "player_x_integrate", "player_y_integrate", "player_tick_timers",
    "X_MIN", "X_MAX", "VIEW_TILES", "TIMER_BYTES", "TIMER_WORD",
]

# [asm 5A4A-5A87] per-frame countdown timers decremented at the tail of the player update, each clamped at 0
# (`sub [x],1 ; adc [x],0` = decrement-but-not-below-zero). Seven byte counters + one word counter.
TIMER_BYTES = (0x6BCE, 0x6BCD, 0x6BEA, 0x6BE8, 0x6BE4, 0x6BE1, 0x6C00)
TIMER_WORD = 0x6BE2

X_MIN = 0x0008          # [asm 5A29] commit only if new_x >= 8 (left world edge)
X_MAX = 0x0FF8          # [asm 5A2E] commit only if new_x < 0xFF8 (right world edge)
VIEW_TILES = 0x14       # [asm 5A20] the viewport width in tiles added to the camera-left tile


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def player_x_integrate(x: int, xvel: int, cam_left: int) -> int:
    """Recover the player horizontal kinematics ``1030:5A0F..5A33``.

    ``new_x = x + sar(xvel, 4)`` (12.4 fixed, arithmetic shift). The move COMMITS only if the new X is inside
    the world bounds AND left of the camera's right edge — otherwise X is unchanged (the player is blocked):

        commit iff  ((cam_left + 0x14) << 4) > new_x  and  8 <= new_x < 0xFF8   (all signed)

    ``cam_left`` is ``[0x8164]`` (camera-left tile). Pure: returns the new ``[0x4F1C]`` value."""
    new_x = (x + (_s16(xvel) >> 4)) & 0xFFFF                  # [5A0F-5A1A] X += sar(Xvel,4)
    bound = ((cam_left + VIEW_TILES) << 4) & 0xFFFF           # [5A1C-5A23] right edge in px
    if _s16(bound) > _s16(new_x) and _s16(new_x) >= X_MIN and _s16(new_x) < X_MAX:  # [5A25/5A29/5A2E]
        return new_x                                         # [5A33] commit
    return x & 0xFFFF                                        # blocked -> unchanged


def player_y_integrate(y: int, yvel: int) -> int:
    """Recover the player vertical kinematics ``1030:5A36..5A3D``.

    ``new_y = y + sar(yvel, 4)`` (12.4 fixed, arithmetic shift). UNCONDITIONAL — unlike the X integrate there
    are no bounds here; the ground/tile collision at ``5A96`` (the very next call) clamps Y and zeroes Yvel on
    contact. Pure: returns the new ``[0x4F1E]`` value."""
    return (y + (_s16(yvel) >> 4)) & 0xFFFF                  # [5A36-5A3D] Y += sar(Yvel,4)


def _dec_floor(v: int, width: int) -> int:
    """One ``sub v,1 ; adc v,0`` saturating decrement (clamps at 0) for an ``width``-bit unsigned counter."""
    mask = (1 << width) - 1
    return (v - 1) & mask if (v & mask) != 0 else 0


def player_tick_timers(timers: dict) -> dict:
    """Recover the player-update timer tail ``1030:5A47..5A87``.

    Decrement each of the seven byte countdown timers + the one word countdown timer, every one clamped at 0.
    Pure: ``timers`` maps each address in ``TIMER_BYTES``/``TIMER_WORD`` to its current value; returns the new
    values (same keys). Bytes are 8-bit-wrapped, the word is 16-bit."""
    out = {a: _dec_floor(timers[a], 8) for a in TIMER_BYTES}     # [5A4A-5A7E] seven byte timers
    out[TIMER_WORD] = _dec_floor(timers[TIMER_WORD], 16)         # [5A82-5A87] one word timer
    return out
