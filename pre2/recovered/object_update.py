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
           "apply_velocity", "ObjectScaleUnsupported", "AnimResult", "advance_animation",
           "FAR_X", "FAR_Y", "EMPTY_ID", "DespawnResult", "despawn_check"]

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


# -- despawn-if-far-from-player (1030:8084, + the 7CFF tail) ------------------------------------------- #

FAR_X = 0x140            # [asm 80A0 cmp ax,0x140] |obj.x - player.x| above this -> far
FAR_Y = 0x12C            # [asm 80AF cmp ax,0x12c] |obj.y - player.y| above this -> far
EMPTY_ID = 0xFFFF        # [asm 80BD/7D06] sprite-id 0xFFFF marks the slot empty (despawned)
_STATE_KEEP = 0xFF       # [asm 808C] state [si+0xE]==0xFF -> never despawn
_DRAWN_BIT = 0x20        # [asm 8092] record flags [si+5] bit5 = drawn (on-screen) -> keep
_STATE_FREE_SLOT = 0x0A  # [asm 80B4] state >= 0xA on a far object -> also free its spawn slot (the 7CFF tail)


def _abs16(d: int) -> int:
    """|d| as the ASM computes it: 16-bit subtract then ``neg`` if the sign bit is set (magnitude 0..0x8000)."""
    d &= 0xFFFF
    return d if d < 0x8000 else (0x10000 - d) & 0xFFFF


class DespawnResult:
    """The contract of one despawn check (1030:8084 + 7CFF): the post-values of the four fields it may write.
    When the object is KEPT the fields equal their inputs (no write)."""
    __slots__ = ("kept", "sprite_id", "def2", "def4", "def7")

    def __init__(self, kept, sprite_id, def2, def4, def7):
        self.kept = kept            # True -> no change (object stays live)
        self.sprite_id = sprite_id  # [si+4]
        self.def2 = def2            # [def+2] (spawn slot; freed only on the far state>=0xA path)
        self.def4 = def4            # [def+4] (behaviour flags; bit2 cleared on despawn)
        self.def7 = def7            # [def+7]

    def __eq__(self, o):
        return (isinstance(o, DespawnResult) and self.sprite_id == o.sprite_id and self.def2 == o.def2
                and self.def4 == o.def4 and self.def7 == o.def7)

    def __repr__(self):
        return (f"Despawn(kept={self.kept}, id={self.sprite_id:#06x}, def2={self.def2:#06x}, "
                f"def4={self.def4:#04x}, def7={self.def7:#04x})")


def despawn_check(obj_x: int, obj_y: int, state: int, flags5: int, old_id: int,
                  player_x: int, player_y: int, def2: int, def4: int, def7: int) -> DespawnResult:
    """The shared per-object "despawn if far from the player" pre-check — recovers ``1030:8084`` and its
    ``7CFF`` tail (every AI handler calls it first). Keep the object (no write) when its state is the keep
    sentinel ``0xFF``, or it is currently drawn (``[si+5]`` bit5), or it is within ``FAR_X``×``FAR_Y`` of the
    player. Otherwise despawn: ``[si+4]=0xFFFF``, ``[def+4]&=0xFB``, ``[def+7]=0``; and for a ``state>=0x0A``
    far object also free its spawn slot ``[def+2]=0xFFFF`` (unless ``[def+4]`` bit1 is set). Returns the
    post-values of the four written fields (unchanged when kept). Pure; the caller owns the records."""
    keep = DespawnResult(True, old_id & 0xFFFF, def2 & 0xFFFF, def4 & 0xFF, def7 & 0xFF)
    if state == _STATE_KEEP:                                  # [asm 808C-8090]
        return keep
    if flags5 & _DRAWN_BIT:                                   # [asm 8092-8096]
        return keep
    far = _abs16(obj_x - player_x) > FAR_X or _abs16(obj_y - player_y) > FAR_Y   # [asm 8098-80B2]
    if not far:
        return keep
    new_def4 = def4 & 0xFB                                    # [asm 80C2 / 7D0B] clear bit2
    new_def2 = def2 & 0xFFFF
    if state >= _STATE_FREE_SLOT and not (new_def4 & 0x02):   # [asm 80B4 jb / 7D0F test 2] free spawn slot
        new_def2 = EMPTY_ID
    return DespawnResult(False, EMPTY_ID, new_def2, new_def4, 0)
