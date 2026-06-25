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

__all__ = ["NO_X_MOVE", "VEL_SHIFT", "FRAME_BASE", "ID_FLAGS_MASK", "FLIP_BIT",
           "apply_velocity", "ObjectScaleUnsupported", "AnimResult", "advance_animation"]

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


# -- animation advance (1030:6881..68E6) --------------------------------------------------------------- #

FRAME_BASE = 0x138       # [asm 689F add dx,0x138] sprite-id base added to the script frame
ID_FLAGS_MASK = 0x6000   # [asm 68E1 and [si+4],0x6000] the blink/opaque flag bits kept across an anim step
FLIP_BIT = 0x8000        # [asm 68DF] sprite-id bit15 = H-flip (from record [si+9] bit7)
_FRAME_MASK = 0x1FFF     # [asm 6891/68B9/68D7 and dh,0x1f] the 13-bit frame field


class ObjectScaleUnsupported(Exception):
    """The animation step ran with a non-zero scale/zoom level ([0x6BE2]!=0) -> the 0xA801 region-remap path
    (the boss zoom), which is NOT yet recovered. Fail loud rather than guess."""


class AnimResult:
    """The contract of one animation-advance step (1030:6881..68E6)."""
    __slots__ = ("sprite_id", "script_ptr", "attr_a340")

    def __init__(self, sprite_id: int, script_ptr: int, attr_a340: int):
        self.sprite_id = sprite_id      # new [si+4] = (old & 0x6000) | frame | flip
        self.script_ptr = script_ptr    # new [si+0xC] (advanced past the consumed frame)
        self.attr_a340 = attr_a340      # the [0xA340] scratch byte the step writes

    def __eq__(self, o):
        return (isinstance(o, AnimResult) and self.sprite_id == o.sprite_id
                and self.script_ptr == o.script_ptr and self.attr_a340 == o.attr_a340)

    def __repr__(self):
        return f"AnimResult(id={self.sprite_id:#06x}, ptr={self.script_ptr:#06x}, a340={self.attr_a340:#04x})"


def advance_animation(script_ptr: int, read_word, old_id: int, flip_byte: int, scale: int) -> AnimResult:
    """Advance one object's animation script — recovers ``1030:6881..68E6`` (the scale==0 path).

    ``script_ptr`` is ``[si+0xC]`` (a DS-relative offset); ``read_word(off)`` reads a 16-bit word from the
    object segment. The script is a list of frame words walked forward each tick; a NEGATIVE word is a relative
    BACK-JUMP (``bx += word``) that loops the animation. The selected frame ``raw`` becomes the new sprite id:
    ``frame = ((raw & 0x1FFF) + 0x138) & 0x1FFF`` then ``[si+4] = (old & 0x6000) | frame | (flip ? 0x8000)``,
    keeping the blink/opaque flag bits; the script pointer advances by 2. Also writes the ``[0xA340]`` scratch
    byte ``((raw>>8)&0xE0)|scale``. ``scale`` is ``[0x6BE2]``; non-zero -> :class:`ObjectScaleUnsupported`
    (the boss zoom region-remap, unrecovered)."""
    bx = script_ptr & 0xFFFF
    for _ in range(256):                                     # [asm 6884-688D] resolve back-jumps
        raw = read_word(bx)
        if raw < 0x8000:                                     # signed >= 0 -> a real frame entry
            break
        bx = (bx + (raw - 0x10000)) & 0xFFFF                 # negative -> relative back-jump (loop)
    else:
        raise ObjectScaleUnsupported("runaway animation back-jump (malformed script)")
    a340 = ((raw >> 8) & 0xE0) | (scale & 0xFF)              # [asm 6891-689B] scratch attribute byte
    if scale != 0:                                           # [asm 68A3 jne -> 0xA801 region remap]
        raise ObjectScaleUnsupported(f"scale [0x6BE2]={scale:#x} region-remap not recovered")
    frame = (((raw & _FRAME_MASK) + FRAME_BASE) & _FRAME_MASK)   # [asm 6891 mask, 689F +0x138, 68D7 mask]
    val = frame | (FLIP_BIT if (flip_byte & 0x80) else 0)    # [asm 68DA-68DF] bit15 = H-flip from [si+9]
    new_id = (old_id & ID_FLAGS_MASK) | val                  # [asm 68E1-68E6] keep blink/opaque flags
    return AnimResult(sprite_id=new_id, script_ptr=(bx + 2) & 0xFFFF, attr_a340=a340)
