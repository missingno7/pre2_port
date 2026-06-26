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

from pre2.recovered.prng import rng_lcg, rng_ror

__all__ = ["NO_X_MOVE", "VEL_SHIFT", "FRAME_BASE", "ID_FLAGS_MASK", "FLIP_BIT", "handle_object_7c90",
           "apply_velocity", "ObjectScaleUnsupported", "AnimResult", "advance_animation",
           "FAR_X", "FAR_Y", "EMPTY_ID", "DespawnResult", "despawn_check", "on_screen_tile",
           "anim_script_rewind", "anim_script_forward", "despawn_full", "dying_state", "saturating_counter",
           "handle_object_7665", "handle_object_773d", "handle_object_77de", "handle_object_7c8c", "handle_object_760f", "handle_object_7c2d", "spawn_effects", "handle_object_7b91", "handle_object_7adf", "orbit_position", "handle_object_7898", "handle_object_75c4", "handle_object_78ec", "terrain_collision"]

NO_X_MOVE = 0xFFFF   # [asm 686C] sentinel in [si+8]: skip the X integrate this frame
VEL_SHIFT = 4        # [asm 6854 cl=4 / 6864 sar ax,cl] velocity is 12.4 fixed point (arithmetic >>4)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


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


# -- on-screen tile-window check (1030:8022) ---------------------------------------------------------- #

ONSCREEN_X = (-2, 0x16)   # [asm 802A/802F] tile X relative to camera must be in [-2, 22] inclusive
ONSCREEN_Y = (-2, 0x0D)   # [asm 803A/803F] tile Y relative to camera must be in [-2, 13] inclusive


def on_screen_tile(x: int, y: int, cam_x: int, cam_y: int) -> bool:
    """Recover ``1030:8022`` — is the pixel ``(x, y)`` within the visible tile window around the camera?
    Tile = ``pixel >> 4`` (arithmetic), then the SIGNED tile offset from the camera (``[0x2DE4]``/``[0x2DE6]``,
    tiles) must satisfy ``-2 <= tx <= 22`` and ``-2 <= ty <= 13``. Returns True (the ASM's CF=0 path) when on
    screen, False (CF=1) otherwise. Pure (the camera is passed in)."""
    tx = _s16(((_s16(x) >> 4) - cam_x) & 0xFFFF)             # [asm 8024-8026]
    if not (ONSCREEN_X[0] <= tx <= ONSCREEN_X[1]):           # [asm 802A-8032 signed jl/jg]
        return False
    ty = _s16(((_s16(y) >> 4) - cam_y) & 0xFFFF)             # [asm 8034-8036]
    return ONSCREEN_Y[0] <= ty <= ONSCREEN_Y[1]              # [asm 803A-8042]


# -- animation-script loop seeks (1030:8048 rewind / 8058 forward) ------------------------------------- #

def anim_script_rewind(script_ptr: int, read_word) -> int:
    """Recover ``1030:8048`` — seek the script pointer BACK to the loop marker: step ``-2`` while the entry is
    non-negative, stop ON the first negative (back-jump) word. Returns the new ``[si+0xC]``. (A handler calls
    this to restart the current animation loop, e.g. the bob oscillator at its top.)"""
    bx = script_ptr & 0xFFFF
    for _ in range(256):                                     # [asm 804C-8051] bx-=2 while [bx] >= 0
        bx = (bx - 2) & 0xFFFF
        if read_word(bx) >= 0x8000:                          # negative -> stop here
            return bx
    raise ObjectScaleUnsupported("runaway anim rewind (no loop marker)")


def anim_script_forward(script_ptr: int, read_word) -> int:
    """Recover ``1030:8058`` — seek the script pointer FORWARD past the loop marker: step ``+2`` while the entry
    is non-negative, then ``+2`` once more past the negative (back-jump) word. Returns the new ``[si+0xC]``."""
    bx = script_ptr & 0xFFFF
    for _ in range(256):                                     # [asm 805C-8063] bx+=2 while [bx] >= 0
        if read_word(bx) >= 0x8000:                          # negative -> step past it [asm 8065]
            return (bx + 2) & 0xFFFF
        bx = (bx + 2) & 0xFFFF
    raise ObjectScaleUnsupported("runaway anim forward (no loop marker)")


# -- per-type AI handler: idx10 @ 1030:7665 ----------------------------------------------------------- #
# A charging/falling enemy state machine on [si+0xE] (settle 0 -> arm 1 -> charge-at-player 2 -> expire 3
# -> dying 0xFF). Reads game-mode [0x2D8A], shake [0x6BEA], anim-ready [0xA340], frame [0x6BD5], player
# [0x4F1C]/[0x4F1E]; def params [def+0xD] charge speed, [def+4] flags, [def+7] timer. Calls despawn_check
# (8084), anim_script_forward (8058), and the full despawn (7CFF). obj/defn/glb are plain dicts.

def despawn_full(obj: dict, defn: dict) -> None:
    """The unconditional full despawn — recovers ``1030:7CFF`` reached as a handler tail: ``[def+7]=0``,
    ``[si+4]=0xFFFF``, ``[def+4]&=0xFB``, and free the spawn slot ``[def+2]=0xFFFF`` unless ``[def+4]`` bit1."""
    defn["d7"] = 0
    obj["id"] = EMPTY_ID
    defn["d4"] &= 0xFB
    if not (defn["d4"] & 0x02):
        defn["d2"] = EMPTY_ID


def handle_object_7665(obj: dict, defn: dict, glb: dict, read_word) -> None:
    """Recover the idx10 AI handler ``1030:7665..773C`` (mutates ``obj``/``defn`` in place).

    ``obj``: x, y, id, xvel, yvel, anim_ptr, state. ``defn``: d2, d4, d7, dD.
    ``glb``: mode([0x2D8A]), shake([0x6BEA]), a340([0xA340]), frame([0x6BD5]), player_x([0x4F1C]),
    player_y([0x4F1E]). ``read_word(off)`` reads the animation script (for anim_script_forward).

    NOTE: the record's "flags5" byte ``[si+5]`` is the HIGH BYTE of the sprite-id word ``[si+4]`` (they
    overlap), so the drawn bit ``[si+5]&0x20`` is ``id & 0x2000`` and despawn_check's flags5 is ``id>>8``."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7665 call 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    st = obj["state"]
    if glb["mode"] == 5 and glb["shake"] != 0 and st != 3 and st != 0xFF:   # [766B-7684] freeze-on-shake
        defn["d7"] = 1

    if st == 0:                                              # [7685] settle: wait until vertical motion stops
        defn["d4"] |= 0x18
        if obj["yvel"] == 0:
            obj["state"] = 1
    elif st == 1:                                            # [7698] arm
        if obj["yvel"] != 0:
            obj["state"] = 0
        else:
            defn["d7"] = 0x1E                                # arm the active timer
            obj["state"] = 2
            obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)
    elif st == 2:                                            # [76B3] charge at the player, then expire
        if abs(_s16(obj["xvel"])) < 0x10:                    # [76B7-76C3] only re-aim when nearly stopped
            if glb["a340"] == 0:                             # [76C5] not anim-ready -> idle this frame
                return
            defn["d4"] = 0x0F                                # [76CC]
            spd = defn["dD"] & 0xFF                          # [76D0] charge speed [def+0xD]
            obj["xvel"] = spd if _s16(obj["x"]) < _s16(glb["player_x"]) else (-spd) & 0xFFFF  # [76D5-76DE]
        if glb["frame"] & 3:                                 # [76E1] count down only every 4th frame
            return
        defn["d7"] = (defn["d7"] - 1) & 0xFF                 # [76E8]
        if defn["d7"] != 0:
            return
        obj["state"] = 3                                     # [76ED] timer expired -> stop + die soon
        obj["yvel"] = 0
        obj["xvel"] = 0xFFFF if (obj["xvel"] & 0x8000) else 0   # [76F8 sar 0xF] keep only the sign
        defn["d4"] = 0x36
        obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)
    elif st == 3:                                            # [7703] wait for the death anim, then despawn
        if glb["a340"] != 0:
            despawn_full(obj, defn)
    elif st == 0xFF:                                         # [7712, == the shared 7CDA] dying
        dying_state(obj, defn, glb)


def dying_state(obj: dict, defn: dict, glb: dict) -> None:
    """The shared 'dying' state — recovers ``1030:7CDA`` (also inlined at 7665's 7712): despawn unless the
    object is "held" (def4 bit0 set) and either drawn (id bit13) or the player is at/below it, in which case
    apply gravity (``yvel += 0xF`` capped at 0xF0). Reached by several handlers' state ``0xFF``."""
    if not (defn["d4"] & 1):                                  # [7CDE] bit0 clear -> despawn
        despawn_full(obj, defn)
    elif (obj["id"] & 0x2000) or (_s16(glb["player_y"]) >= _s16(obj["y"])):  # [7CE4-7CF1]
        if _s16(obj["yvel"]) < 0xF0:                          # [7CF3] gravity, capped at 0xF0
            obj["yvel"] = (obj["yvel"] + 0xF) & 0xFFFF
    else:
        despawn_full(obj, defn)


def handle_object_773d(obj: dict, defn: dict, glb: dict, read_word=None) -> None:
    """Recover the idx9 AI handler ``1030:773D..77DD`` — a horizontal-patrol enemy that accelerates back and
    forth between def bounds, with its OWN proximity despawn (it does NOT call the shared 8084).

    ``obj``: x, y, id, xvel, yvel, state. ``defn``: d4, dD/dE (left bound, 16-bit), dF/d10 (right bound,
    16-bit), d11 (signed-byte speed, mutated), d12 (speed magnitude limit). ``glb``: player_x, player_y."""
    # the patrol bounds are 16-bit ([def+0xD]=left, [def+0xF]=right); reconstruct from the byte fields (this
    # def offset is a byte union — idx4 reads [def+0xD]/[def+0xF] as bytes, idx9 as the low halves of words).
    left = _s16((defn["dD"] | (defn["dE"] << 8)) & 0xFFFF)
    right = _s16((defn["dF"] | (defn["d10"] << 8)) & 0xFFFF)
    drawn = obj["id"] & 0x2000
    st = obj["state"]
    if not drawn and st != 0xFF:                             # [7740-7784] proximity despawn (skip if drawn/dying)
        if _abs16(obj["y"] - glb["player_y"]) >= 0xBE:       # [774C-775A] too far vertically -> despawn
            keep = False
        else:
            px = _s16(glb["player_x"])
            keep = (right + 0x1E0 > px) if px >= left else (px + 0x1E0 >= left)   # [775C-7779] horizontal window
        if not keep:
            obj["id"] = EMPTY_ID                             # [777B] despawn ([si+4]=0xFFFF, [def+4]&=0xFB)
            defn["d4"] &= 0xFB
            return

    if st == 0:                                              # [7785] patrol right: accelerate +3 up to +d12
        sp = _s8(defn["d11"])
        obj["xvel"] = sp & 0xFFFF                            # [7790] Xvel = current speed
        if sp + 3 <= (defn["d12"] & 0xFF):                   # [7793-779D] cap at [def+0x12]
            defn["d11"] = (sp + 3) & 0xFF
        if right < _s16(obj["x"]):                           # [77A2-77A9] past the right bound -> turn
            obj["state"] = 1
    elif st == 1:                                            # [77AE] patrol left: accelerate -3 down to -d12
        sp = _s8(defn["d11"])
        obj["xvel"] = sp & 0xFFFF
        if sp - 3 >= -(defn["d12"] & 0xFF):                  # [77B9-77C5] cap at -[def+0x12]
            defn["d11"] = (sp - 3) & 0xFF
        if left >= _s16(obj["x"]):                           # [77CA-77D1] past the left bound -> turn
            obj["state"] = 0
    elif st == 0xFF:                                         # [77DA jmp 7CDA]
        dying_state(obj, defn, glb)


# -- more shared helpers + handlers ------------------------------------------------------------------- #

def saturating_counter(def6: int, def7: int) -> tuple[int, bool]:
    """Recover ``1030:8001`` — saturating-increment the timer ``[def+7]`` (caps at 0xFF), and report whether
    ``([def+7] >> 2) >= [def+6]`` (the ASM's ``cmp``; the caller branches ``jb``=not-ready / ``jae``=ready).
    Returns ``(new_def7, ready)``."""
    nd7 = def7 + 1 if def7 < 0xFF else 0xFF                  # [asm 8001 add+sbb = saturate]
    ready = ((nd7 >> 2) & 0xFF) >= (def6 & 0xFF)             # [asm 8009-8010]
    return nd7, ready


def handle_object_7c8c(obj: dict, defn: dict, glb: dict, read_word=None) -> None:
    """idx1 handler ``1030:7C8C`` = ``call 8084; ret`` — a passive object that only despawns when far."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7


def _pounce(obj: dict, defn: dict, glb: dict, read_word) -> None:
    """The pounce kick-off shared by idx8 states 0 and 0xC (1030:7827)."""
    obj["yvel"] = (-((_s8(defn["dE"]) << 4))) & 0xFFFF       # [7827] jump up by [def+0xE]<<4
    spd = (_s8(defn["dF"]) << 4) & 0xFFFF                    # [7832] horizontal pounce speed [def+0xF]<<4
    obj["xvel"] = spd if _s16(glb["player_x"]) > _s16(obj["x"]) else (-spd) & 0xFFFF   # [7838-7842] toward player
    obj["state"] = 0xA                                       # [7845]
    defn["d4"] = (defn["d4"] & 0xD3) | 0x2C                  # [7849-7850]
    obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)   # [7853]


def handle_object_77de(obj: dict, defn: dict, glb: dict, read_word) -> None:
    """Recover the idx8 AI handler ``1030:77DE..7897`` — a POUNCING enemy: wait (timer ``8001``) facing the
    player, and when the player is within ``[def+0xD]`` (tile X) × ``[def+0x10]`` (tile Y) leap up+toward them
    (``[def+0xE]`` height / ``[def+0xF]`` speed); states 0xA rise → 0xB land → 0xC cooldown → pounce again.

    ``obj``: x, y, id, xvel, yvel, anim_ptr, state. ``defn``: d2, d4, d6, d7, dD, dE, dF, d10. ``glb``:
    player_x, player_y. ``read_word`` for the anim seeks (8058/8048)."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [77DE call 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    if obj["xvel"] == 0:                                     # [77E3-77F5] face the player when stationary
        obj["xvel"] = 1 if _s16(obj["x"]) <= _s16(glb["player_x"]) else (-1) & 0xFFFF
    st = obj["state"]
    if st == 0:                                             # [77FF] wait, then pounce when the player is near
        defn["d7"], ready = saturating_counter(defn["d6"], defn["d7"])
        if not ready:                                       # [7802 jb 7825]
            return
        dist_x = (_abs16(glb["player_x"] - obj["x"]) >> 4) & 0xFF   # [7804-780F]
        if (defn["dD"] & 0xFF) < dist_x:                    # [780F jb 7825] out of X range
            return
        dist_y = (_abs16(glb["player_y"] - obj["y"]) >> 4) & 0xFF   # [7814-7820]
        if (defn["d10"] & 0xFF) >= dist_y:                  # [7820 jae 7827] in Y range -> pounce
            _pounce(obj, defn, glb, read_word)
    elif st == 0xA:                                         # [7857] rising -> until apex (yvel >= 0)
        if _s16(obj["yvel"]) >= 0:
            obj["state"] = 0xB
            obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)
    elif st == 0xB:                                         # [7869] falling -> until landed (yvel <= 0)
        if _s16(obj["yvel"]) <= 0:
            obj["state"] = 0xC
            obj["anim_ptr"] = anim_script_rewind(anim_script_rewind(obj["anim_ptr"], read_word), read_word)
            obj["xvel"] = 0                                 # [787D]
            defn["d7"] = 0                                  # [7882] restart the cooldown timer
    elif st == 0xC:                                         # [7887] cooldown -> pounce again when ready
        defn["d7"], ready = saturating_counter(defn["d6"], defn["d7"])
        if ready:                                           # [788E jae 7827]
            _pounce(obj, defn, glb, read_word)
    elif st == 0xFF:                                        # [7890 jmp 7CDA]
        dying_state(obj, defn, glb)


def handle_object_7c90(obj: dict, defn: dict, glb: dict, read_word) -> None:
    """Recover the idx0 AI handler ``1030:7C90..7CD9`` — a ground enemy/collectible that, once the player is
    near, flags itself for the walker's tile-collision pass (``[def+4]|=8``) then chases on the ground.

    state 0: wait on the ``8001`` timer; despawn if the player is >= 0xB0 above (signed ``objY-playerY``);
    else set the collide flag + state 1. state 1: once vertical motion stops (``yvel==0``), pick a horizontal
    speed toward the player (±0x20) + anim-forward + state 2. state 2: idle. state 0xFF: dying.

    ``obj``: x, y, id, xvel, yvel, anim_ptr, state. ``defn``: d2, d4, d6, d7. ``glb``: player_x, player_y."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7C90 call 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    st = obj["state"]
    if st == 0:                                             # [7C96]
        defn["d7"], ready = saturating_counter(defn["d6"], defn["d7"])   # [7C9A call 8001]
        if not ready:
            return
        if _s16(obj["y"] - glb["player_y"]) >= 0xB0:        # [7C9F-7CA9 jge 7CFF] far below player -> despawn
            despawn_full(obj, defn)
        else:
            defn["d4"] |= 8                                 # [7CAB] flag for the walker's 698C tile collision
            obj["state"] = 1
    elif st == 1:                                           # [7CB4]
        if obj["yvel"] == 0:                                # [7CB8] until vertical motion stops
            obj["state"] = 2
            obj["xvel"] = 0x20 if _s16(obj["x"]) < _s16(glb["player_x"]) else (-0x20) & 0xFFFF   # [7CC2-7CCE]
            obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)   # [7CD1]
    elif st == 2:                                           # [7CD5] idle
        pass
    elif st == 0xFF:
        dying_state(obj, defn, glb)


def handle_object_760f(obj: dict, defn: dict, glb: dict, read_word=None) -> None:
    """Recover the idx11 AI handler ``1030:760F..7664`` — the LEAPING enemy (flying squirrel). When anim-ready
    it leaps toward the player (Xvel = ±``[def+0xD]``<<4) and up (Yvel = -``[def+0xE]``<<4, state 1), then
    falls under gravity (Yvel += 8 each frame until it reaches the terminal ``[def+0xF]``<<4).

    ``obj``: x, id, xvel, yvel, state. ``defn``: d2, d4, d7, dD, dE, dF. ``glb``: a340, player_x, player_y."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7611 call 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    st = obj["state"]
    if st == 0:                                            # [7617] wait for anim, then leap
        if glb["a340"] == 0:                              # [761B]
            return
        obj["state"] = 1
        defn["d4"] &= 0xEF                                # [7626]
        leap = (_s8(defn["dD"]) << 4) & 0xFFFF            # [762A-7630] horizontal leap toward player
        obj["xvel"] = leap if _s16(glb["player_x"]) > _s16(obj["x"]) else (-leap) & 0xFFFF
        obj["yvel"] = (-(_s8(defn["dE"]) << 4)) & 0xFFFF  # [763D-7645] jump up
    elif st == 1:                                         # [7649] gravity until terminal velocity
        if _s16(obj["yvel"]) < _s16((_s8(defn["dF"]) << 4) & 0xFFFF):   # [764D-7656]
            obj["yvel"] = (obj["yvel"] + 8) & 0xFFFF
    elif st == 0xFF:                                      # [7661 jmp 7712 == dying_state]
        dying_state(obj, defn, glb)


# -- idx2 vertical-bob oscillator (1030:7C2D) + its effect spawner (1030:7FD9) -------------------------- #

def spawn_effects(def9: int, defB: int, arg: int, dl: int, find_free) -> list:
    """Recover ``1030:7FD9`` — spawn 3 entries into the secondary effect list (``0x7DE6``). Each 6-byte entry:
    ``[0]=X=[def+9]``, ``[2]=Y=[def+0xB]-0x18``, ``[4]=arg``, ``[5]=angle = (k+1)*(dl>>2)`` (signed byte shift).
    ``find_free()`` yields the next writable slot (a length>=4 mutable sequence ``[x, y, b4, b5]``); the live
    caller scans ``0x7DE6`` for the first ``x==0xFFFF``. Returns the 3 spawned ``(x, y, b4, b5)`` tuples. It does
    NOT touch the object record/def, so a handler's obj/def contract is independent of it."""
    step = (_s8(dl) >> 2) & 0xFF                          # [7FD9 sar dl,1 x2]
    y = (defB - 0x18) & 0xFFFF                            # [7FEF]
    out, ang = [], step
    for _ in range(3):                                   # [7FE1 bp=3]
        slot = find_free()                               # [7FE4 call 8014]
        slot[0], slot[1], slot[2], slot[3] = def9 & 0xFFFF, y, arg & 0xFF, ang & 0xFF
        out.append((def9 & 0xFFFF, y, arg & 0xFF, ang & 0xFF))
        ang = (ang + step) & 0xFF                         # [7FFB ch += ah]
    return out


def handle_object_7c2d(obj: dict, defn: dict, glb: dict, read_word=None, spawn=None) -> None:
    """Recover the idx2 AI handler ``1030:7C2D..7C8B`` — a vertical BOB oscillator that floats down/up around
    ``[def+0xB]`` within amplitude ``[def+0xD]`` at speed ``[def+0xE]``, trailing effects (``7FD9``) each frame.

    ``obj``: x, y, id, yvel, anim_ptr, state. ``defn``: d2, d4, d7, d9, dB, dD, dE. ``glb``: player_x, player_y.
    ``spawn`` (optional) is the recovered :func:`spawn_effects` bound to the effect list; the obj/def contract
    is independent of it (the live walker passes it; the shadow leaves it None)."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7C2D call 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    st = obj["state"]
    if st >= 2:                                           # [7C35 cmp al,2; jae 7C85]
        if st == 0xFF:
            dying_state(obj, defn, glb)
        return
    rel_y = (obj["y"] - defn["dB"]) & 0xFFFF              # [7C3A] distance below the bob centre
    if spawn is not None:
        spawn(defn["d9"], defn["dB"], 0, rel_y & 0xFF)    # [7C40-7C42] al=0, dl=rel_y low byte
    if st == 0:                                           # [7C4A] moving DOWN
        obj["yvel"] = (_s8(defn["dE"] & 0xFF) << 4) & 0xFFFF
        if _s8(defn["dD"] & 0xFF) < _s8(rel_y & 0xFF):    # [7C59 cmp [def+0xD],al; jge -> ret] amplitude reached
            obj["state"] = 1
            obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)
    else:                                                # [7C66] state 1, moving UP
        obj["yvel"] = (-(_s8(defn["dE"] & 0xFF) << 4)) & 0xFFFF
        if _s16(rel_y) < 0:                               # [7C7B jge -> ret] rose above the centre
            obj["state"] = 0
            obj["anim_ptr"] = anim_script_rewind(obj["anim_ptr"], read_word)


def handle_object_7b91(obj: dict, defn: dict, glb: dict, read_word=None, spawn=None, tile_prop=None) -> None:
    """Recover the idx3 AI handler ``1030:7B91..7C2C`` — a FALLING/LANDING enemy. state 0: wait until the
    player is within ``[def+0xD]`` tiles (horizontally, of the spawn X ``[def+9]``), then start falling
    (Yvel=0x20). state 1: trail effects (``7FD9``) + check the level tile under it; once it hits solid ground
    snap to the tile, stop, flag for collision (``[def+4]|=0x48``), dash toward the player (±0x30), state 2.
    state 2: bounce its X velocity if it wraps off the left edge. state 0xFF: dying.

    ``obj``: x, y, id, xvel, yvel, anim_ptr, state. ``defn``: d2, d4, d6, d7, d9, dB, dD. ``glb``: player_x,
    player_y. ``tile_prop(tile_x, tile_y) -> int`` returns the terrain property under that tile (the live
    level-map lookup ``[0x7F5E + map[tileY*0x100+tileX]]``); ``spawn`` is the optional effect spawner."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7B91 call 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    st = obj["state"]
    if st == 0:                                            # [7B99] wait for the player to approach
        defn["d7"], ready = saturating_counter(defn["d6"], defn["d7"])
        if not ready:
            return
        dist_x = (_abs16(defn["d9"] - glb["player_x"]) >> 4) & 0xFF   # [7BA2-7BAD]
        if (defn["dD"] & 0xFF) < dist_x:                   # [7BAF jb 7C2C] still too far -> wait
            return
        defn["d4"] &= 0xEF                                 # [7BB4]
        obj["state"] = 1
        obj["yvel"] = 0x20                                 # [7BBC] start falling
        obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)
    elif st == 1:                                          # [7BC5] fall until it lands on solid ground
        if spawn is not None:
            spawn(defn["d9"], defn["dB"], 0, (obj["y"] - defn["dB"]) & 0xFF)   # [7BC9-7BD1]
        prop = tile_prop((obj["x"] >> 4) & 0xFF, (obj["y"] >> 4) & 0xFF) if tile_prop else 0  # [7BD4-7BEB]
        if prop == 0:                                      # [7BEC je 7C2C] no ground yet -> keep falling
            return
        obj["y"] &= 0xFFF0                                 # [7BF0] snap to the tile
        obj["yvel"] = 0                                    # [7BF4]
        obj["state"] = 2                                   # [7BF9]
        defn["d4"] |= 0x48                                 # [7C00]
        obj["xvel"] = 0x30 if _s16(glb["player_x"]) >= _s16(obj["x"]) else (-0x30) & 0xFFFF   # [7C04-7C11]
        obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)
    elif st == 2:                                          # [7C18] bounce off the left world edge
        if _s16(obj["x"]) < 0:
            obj["xvel"] = (-_s16(obj["xvel"])) & 0xFFFF
    elif st == 0xFF:                                       # [7C29 jmp 7CDA]
        dying_state(obj, defn, glb)


# -- idx4 orbit/pendulum enemy (1030:7ADF) + its position helper (1030:7B53) ---------------------------- #

def orbit_position(center_x: int, center_y: int, radius: int, cos_val: int, sin_val: int) -> tuple:
    """Recover ``1030:7B53``'s position math: ``X = centreX + ((cos>>2)*radius)>>4`` and ``Y`` likewise with
    ``sin`` — all signed. ``cos_val``/``sin_val`` are the signed table bytes ``[0x6F90+angle]``/``[0x7090+angle]``;
    ``radius`` is ``[def+0xD]`` (signed byte)."""
    r = _s8(radius)
    x = (center_x + (((_s8(cos_val) >> 2) * r) >> 4)) & 0xFFFF      # [7B5A-7B6C]
    y = (center_y + (((_s8(sin_val) >> 2) * r) >> 4)) & 0xFFFF      # [7B79-7B84]
    return x, y


def handle_object_7adf(obj: dict, defn: dict, glb: dict, read_word=None, spawn=None,
                       cos_table=None, sin_table=None) -> None:
    """Recover the idx4 AI handler ``1030:7ADF..7B52`` — an ORBIT/PENDULUM enemy. Despawns by its ORBIT CENTRE
    (``[def+9]/[def+0xB]``, the ``8089`` entry into despawn_check). state 0: descend (objY += 2, trailing
    effects) until ``objY-centreY >= [def+0xD]``; state 1: spin the angle ``[def+0xF]`` up by 4/frame to
    ``[def+0xE]``; state 2: pendulum SHM (``[def+0x10] += -sign(angle)``, ``angle += [def+0x10]``). In states
    1/2 the position is set by the orbit (``7B53``). state 0xFF: dying.

    ``obj``: x, y, id, state. ``defn``: d2, d4, d6, d7, d9(centreX), dB(centreY), dD(radius), dE(max angle),
    dF(angle byte), d10(ang-vel byte). ``cos_table``/``sin_table`` are ``angle -> signed table byte`` callbacks
    (the live ``[0x6F90+angle]``/``[0x7090+angle]``); ``spawn`` optional."""
    dr = despawn_check(defn["d9"], defn["dB"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7AE8 8089: by centre]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7

    def _orbit():                                          # [7B53] set the position on the orbit + trail
        if cos_table is not None and sin_table is not None:
            ang = defn["dF"] & 0xFF
            obj["x"], obj["y"] = orbit_position(defn["d9"], defn["dB"], defn["dD"], cos_table(ang), sin_table(ang))
        if spawn is not None:
            spawn(defn["d9"], defn["dB"], defn["dF"] & 0xFF, defn["dD"] & 0xFF)   # [7B87] al=angle, dl=radius

    st = obj["state"]
    if st == 0:                                           # [7AF0] descend to the orbit start
        defn["d7"], ready = saturating_counter(defn["d6"], defn["d7"])
        if not ready:
            return
        rel_y = (obj["y"] - defn["dB"]) & 0xFFFF
        if spawn is not None:
            spawn(defn["d9"], defn["dB"], 0, rel_y & 0xFF)   # [7B00] 7FD9
        if (defn["dD"] & 0xFF) <= (rel_y & 0xFF):         # [7B06 jbe] reached depth -> orbit
            obj["state"] = 1
        else:
            obj["y"] = (obj["y"] + 2) & 0xFFFF            # [7B0B]
    elif st == 1:                                         # [7B15] spin up the angle
        _orbit()
        defn["dF"] = (defn["dF"] + 4) & 0xFF              # [7B1C]
        if (defn["dF"] & 0xFF) >= (defn["dE"] & 0xFF):    # [7B23 jb -> ret] clamp + state 2
            defn["dF"] = defn["dE"] & 0xFF
            obj["state"] = 2
    elif st == 2:                                         # [7B30] pendulum oscillation
        _orbit()
        dl = 1 if _s8(defn["dF"]) < 0 else (-1)           # [7B37-7B3F] restoring direction
        defn["d10"] = (defn["d10"] + dl) & 0xFF           # [7B41]
        defn["dF"] = (defn["dF"] + _s8(defn["d10"])) & 0xFF   # [7B47] angle += ang-vel
    elif st == 0xFF:                                      # [7B4F jmp 7CDA]
        dying_state(obj, defn, glb)


def handle_object_7898(obj: dict, defn: dict, glb: dict, read_word=None) -> None:
    """Recover the idx7 AI handler ``1030:7898..78EB`` — a creeper/LEAPER enemy. Shared far-despawn first
    (``8084``); then state 0 faces the player horizontally (``Xvel = ±1``) and, once the player is within
    ``[def+0xD]`` horizontal tiles (``|player_x-objX|>>4``, compared as a BYTE), LEAPS: state -> 0xA,
    ``Yvel = [def+0xE]<<4`` (signed), ``Xvel = ±that`` mirrored by the facing, and the anim advances. state
    0xA just flies (no change); 0xFF dies.

    ``obj``: x, y, id, xvel, yvel, state, anim_ptr. ``defn``: d2, d4, d7, dD (range byte), dE (leap speed,
    signed byte). ``glb``: player_x, player_y."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [7898 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7
    st = obj["state"]
    if st == 0:                                              # [789D-78A2]
        dx = 1 if _s16(obj["x"]) <= _s16(glb["player_x"]) else (-1)   # [78A4-78AE jle] face the player
        obj["xvel"] = dx & 0xFFFF                            # [78B0]
        dist = (_abs16(glb["player_x"] - obj["x"]) >> 4) & 0xFF       # [78B3-78BC] |dx|>>4 low byte
        if (defn["dD"] & 0xFF) < dist:                       # [78BE-78C1 jb -> ret] out of range
            return
        obj["state"] = 0xA                                   # [78C3] launch
        leap = (_s8(defn["dE"]) << 4) & 0xFFFF               # [78C7-78CB] signed [def+0xE]<<4
        obj["yvel"] = leap                                   # [78CD]
        if ((obj["xvel"] >> 8) & 0x50) != 0:                 # [78D0-78D6] facing-left (Xvel hi byte) -> mirror X
            leap = (-leap) & 0xFFFF
        obj["xvel"] = leap                                   # [78D8]
        obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)   # [78DB 8058]
    elif st == 0xFF:                                         # [78E4-78E8]
        dying_state(obj, defn, glb)
    # state 0xA and any other -> no change [78DF-78E3, 78EB ret]


def handle_object_75c4(obj: dict, defn: dict, glb: dict, read_word=None) -> None:
    """Recover the idx12 AI handler ``1030:75C4..760E`` — a falling / earthquake-spawned object. state 0 sets
    ``Xvel = ±([def+0xD]<<4)`` toward the player (sign by ``player_x >= objX``) and advances to state 1. state 1
    keeps the off-screen timer ``[def+7]`` at 0 while the object is drawn; once it has been off-screen long
    enough (``[def+7]`` reaches 0x9A) it flips to state 0xFF. state 0xFF dies.

    ``obj``: x, id, xvel, state. ``defn``: dD (signed-byte speed), d7 (off-screen timer). ``glb``: player_x."""
    st = obj["state"]
    if st == 0:                                              # [75C9-75CE]
        ax = _s8(defn["dD"])                                 # [75D0-75D3] signed [def+0xD]
        if _s16(glb["player_x"]) < _s16(obj["x"]):           # [75D6-75DC jge skips neg] player to the left -> -spd
            ax = -ax
        obj["xvel"] = (ax << 4) & 0xFFFF                     # [75DE-75E0]
        obj["state"] = 1                                     # [75E3]
    elif st == 1:                                            # [75E8-75EA]
        if obj["id"] & 0x2000:                               # [75EC-75F0] drawn (on-screen) -> reset timer
            defn["d7"] = 0                                   # [7601]
        else:
            defn["d7"] = (defn["d7"] + 1) & 0xFF             # [75F2]
            if defn["d7"] >= 0x9A:                           # [75F5-75F9] off-screen long enough -> die
                obj["state"] = 0xFF                          # [75FB]
    elif st == 0xFF:                                         # [7607-7609]
        dying_state(obj, defn, glb)                          # [760B 7CDA]


def handle_object_78ec(obj: dict, defn: dict, glb: dict, read_word=None) -> None:
    """Recover the idx6 AI handler ``1030:78EC..7A47`` — the EARTHQUAKE / screen-shake driver.

    Shared far-despawn first (``8084``). state 0 faces the player (``Xvel=±1``) and, once the player is within
    ``[def+0xD]`` horizontal tiles, arms: state->1, resets the shake counters and seeds the position
    accumulators ``[def+0x11]/[def+0x13]`` (16-bit, byte-pair little-endian over ``d11/d12`` and ``d13/d14``)
    to ``objX<<3 / objY<<3``. state 1 each frame: faces the player, computes ``dist² = dX²+dY²`` into the
    scratch ``[0xA30E:0xA310]``; if the player is close (``dist² < 0x2710``, high word 0) and quakes are enabled
    (``[0x6BD0]``) it kicks a directional shake velocity (``[def+0xE]`` or ``[def+0xF]``); otherwise it counts
    the shake timer ``[def+0x10]`` down and, on underflow, RANDOM-reseeds the shake (two PRNGs, the global
    amplitudes ``[0x6BC0]/[0x6BC1]`` and the velocities). Every state-1 path then ACCUMULATES the position
    (``7A28``): ``[def+0x11]+=s8([def+0xE]); [def+0x13]+=s8([def+0xF]); objX=[def+0x11]>>3; objY=[def+0x13]>>3``.
    state 0xFF dies.

    ``obj``: x, y, id, xvel, state. ``defn``: d2, d4, d7, dD, dE, dF, d10, d11/d12 (X accum word), d13/d14
    (Y accum word). ``glb``: player_x, player_y, a30e, a310 (dist² scratch), bc0, bc1 (shake amplitudes),
    bd0 (quake-enable flag), ror (``[0x28C1]``), la, lb, lc, ld (the ``rng_lcg`` state words)."""
    dr = despawn_check(obj["x"], obj["y"], obj["state"], (obj["id"] >> 8) & 0xFF, obj["id"],   # [78EC 8084]
                       glb["player_x"], glb["player_y"], defn["d2"], defn["d4"], defn["d7"])
    obj["id"], defn["d2"], defn["d4"], defn["d7"] = dr.sprite_id, dr.def2, dr.def4, dr.def7

    def face():                                              # [7A50]
        obj["xvel"] = (1 if _s16(obj["x"]) <= _s16(glb["player_x"]) else (-1)) & 0xFFFF

    def accumulate():                                        # [7A28] drive the position from the accumulators
        w11 = ((defn["d11"] | (defn["d12"] << 8)) + _s8(defn["dE"])) & 0xFFFF
        w13 = ((defn["d13"] | (defn["d14"] << 8)) + _s8(defn["dF"])) & 0xFFFF
        defn["d11"], defn["d12"] = w11 & 0xFF, (w11 >> 8) & 0xFF
        defn["d13"], defn["d14"] = w13 & 0xFF, (w13 >> 8) & 0xFF
        obj["x"] = (_s16(w11) >> 3) & 0xFFFF
        obj["y"] = (_s16(w13) >> 3) & 0xFFFF

    st = obj["state"]
    if st == 0:                                              # [78F1-78F6]
        face()
        dist = (_abs16(glb["player_x"] - obj["x"]) >> 4) & 0xFF      # [78FB-7904]
        if (defn["dD"] & 0xFF) >= dist:                      # [7906-7909 jae] in range -> arm
            obj["state"] = 1                                 # [790E]
            defn["d10"] = 0x18                               # [7912]
            defn["dE"] = 0                                   # [7916]
            defn["dF"] = 0                                   # [791A]
            w11 = (_s16(obj["x"]) << 3) & 0xFFFF             # [791E-7924]
            w13 = (_s16(obj["y"]) << 3) & 0xFFFF             # [7927-792C]
            defn["d11"], defn["d12"] = w11 & 0xFF, (w11 >> 8) & 0xFF
            defn["d13"], defn["d14"] = w13 & 0xFF, (w13 >> 8) & 0xFF
        # else out of range [790B jmp 7A4F] -> ret, no change
    elif st == 1:                                            # [7930-7937]
        face()
        dx = _s16(glb["player_x"] - obj["x"])                # [793A-793F] bp = dX (signed)
        dy = _s16(glb["player_y"] - obj["y"])                # [794F-7952]
        adx, ady = abs(dx), abs(dy)
        dist_sq = adx * adx + ady * ady                      # [7945-7961] dX² + dY² (32-bit)
        glb["a30e"], glb["a310"] = dist_sq & 0xFFFF, (dist_sq >> 16) & 0xFFFF
        close = glb["a310"] == 0 and glb["a30e"] < 0x2710 and (glb["bd0"] & 0xFF) != 0   # [7965-7979]
        if close:                                            # [797B-799C] directional shake kick
            al = 0x68 if glb["a30e"] < 0xE10 else 0x40       # [797B-7985]
            if (ady & 0xFFFF) >= 0x30:                       # [7987-798A jae] big vertical sep -> shake Y
                defn["dF"] = 0xC0                            # [7998]
            else:
                if dx > 0:                                   # [798C-798E jle skips neg] dX>0 -> mirror
                    al = (-al) & 0xFF                        # [7990]
                defn["dE"] = al & 0xFF                       # [7992]
        else:                                                # [799F] count the shake timer down
            defn["d10"] = (defn["d10"] - 1) & 0xFF           # [799F dec]
            if defn["d10"] & 0x80:                           # [79A2 js] underflow -> random reseed
                r = rng_ror(glb["ror"]); glb["ror"] = r      # [79A7 26CF]
                defn["d10"] = ((r & 0xF) + 3) & 0xFF         # [79AA-79AE]
                glb["la"], glb["lb"], glb["lc"], glb["ld"], ret = rng_lcg(            # [79B1 39DF]
                    glb["la"], glb["lb"], glb["lc"], glb["ld"])
                al = ret & 0xF                               # [79B4-79B9] (ah==0 so +ah is a no-op)
                if _s16(glb["player_x"]) < _s16(obj["x"]):   # [79BB-79C3 jge skips neg]
                    al = (-al) & 0xFF
                glb["bc0"] = al & 0xFF                       # [79C5]
                glb["la"], glb["lb"], glb["lc"], glb["ld"], ret = rng_lcg(            # [79C8 39DF]
                    glb["la"], glb["lb"], glb["lc"], glb["ld"])
                al = ret & 0xF                               # [79CB]
                if _s16(glb["player_y"]) < _s16((obj["y"] + 0x32) & 0xFFFF):          # [79CD-79D9 jge]
                    al = (-al) & 0xFF
                glb["bc1"] = al & 0xFF                       # [79DB]
                r = rng_ror(glb["ror"]); glb["ror"] = r      # [79DE 26CF]
                al = r & 0x1F                                # [79E1]
                if _s8(glb["bc0"]) < 0:                       # [79E3-79EA jge]
                    al = (-al) & 0xFF
                defn["dE"] = al & 0xFF                       # [79EC]
                if (glb["bc0"] & 0xFF) == 0:                  # [79EF-79F8]
                    al = 2
                else:                                        # [79FA-7A14] al = (|bc1|*8) / |bc0|
                    ch = abs(_s8(glb["bc0"])) & 0xFF
                    ax = (abs(_s8(glb["bc1"])) << 3) & 0xFFFF
                    al = (ax // ch) & 0xFF
                if (al & 0xFF) >= 0x20:                       # [7A16-7A1A jb skips clamp]
                    al = 0x20
                if _s8(glb["bc1"]) < 0:                       # [7A1C-7A23 jge]
                    al = (-al) & 0xFF
                defn["dF"] = al & 0xFF                        # [7A25]
            # else (no underflow) -> just accumulate
        accumulate()                                         # [7995/799C/79A4 jmp 7A28; reseed falls through]
    elif st == 0xFF:                                         # [7A48-7A4A]
        dying_state(obj, defn, glb)                          # [7A4C 7CDA]


# -- terrain collision (1030:698C + slope helper 6A7D) ------------------------------------------------- #

def _surface_offset(obj_x: int, tile: int, slope) -> int:
    """Recover the slope-height helper ``1030:6A7D`` — the sub-tile Y offset for a (possibly sloped) ground
    tile. ``slope(tile)`` is the live ``[0x8E1D + tile]`` byte: bit5/bit4 (``0x30``) mark a slope, the low
    nibble is the base height, bit4 (``0x10``) the slope direction. Flat tiles (no ``0x30`` bits) return the
    raw byte sign-extended. Sloped tiles interpolate by ``(objX & 0xF) // 3``."""
    s = slope(tile) & 0xFF                                   # [6A7F-6A81]
    if (s & 0x30) == 0:                                      # [6A83 test 0x30; je]
        return _s8(s)                                        # [6AA5 cwde] flat: raw height
    q = (obj_x & 0xF) // 3                                   # [6A89-6A90] div bl=3 -> quotient
    if s & 0x10:                                             # [6A92 test 0x10; je]
        al = (q + (s & 0xF)) & 0xFF                          # [6A97] rising
    else:
        al = (((-q) & 0xFF) + (s & 0xF)) & 0xFF              # [6A9E neg al; add dl] falling
    return _s8(al)                                           # [6AA5 cwde]


def _settle(obj: dict, defn: dict, tile: int, slope) -> None:
    """The landing tail ``1030:6A3E..6A6A`` — snap Y onto the ground tile's surface and resolve the vertical
    velocity (stop if ``[def+4]&0x20``, else bounce at ``-Yvel/2`` when fast enough, else stop)."""
    off = _surface_offset(obj["x"], tile, slope)             # [6A40 6A7D]
    obj["y"] = ((obj["y"] & 0xFFF0) + off) & 0xFFFF          # [6A43-6A47] snap to surface
    if defn["d4"] & 0x20:                                    # [6A4D test 0x20; jne]
        obj["yvel"] = 0                                      # [6A65] non-bouncing -> stop
    else:
        dx = _s16((-_s16(obj["yvel"])) & 0xFFFF) >> 1        # [6A53-6A5A] neg, arithmetic sar 1
        obj["yvel"] = (dx & 0xFFFF) if abs(dx) > 0x20 else 0  # [6A5E-6A67] bounce if fast enough


def terrain_collision(obj: dict, defn: dict, read_map, prop_a, prop_b, slope, read_word) -> None:
    """Recover ``1030:698C`` — the per-object level-map TERRAIN COLLISION (walker calls it when ``[def+4]&8``).

    Looks up three map tiles around the object — here ``(tx,ty)``, above ``(tx,ty-1)``, and ahead-above
    ``(tx±1,ty-1)`` by the X-velocity sign — using ``read_map(tile_index)`` over the level map seg ``[0x2DDA]``
    (tile_index = ``((objY>>4)&0xFF)<<8 | ((objX>>4)&0xFF)``). It resolves a horizontal collision / wall-climb
    against the "ahead" tile's property ``prop_a`` (``[0x7E5E+tile]``) gated by the ``[def+4]`` 0x40/0x80
    climb-state bits, then a vertical/ground collision against ``prop_b`` (``[0x7F5E+tile]``): solid -> settle
    onto the surface (slope-aware), empty -> apply gravity. Mutates ``obj`` x/y/xvel/yvel/anim_ptr and the
    ``[def+4]`` flags. ``prop_a/prop_b/slope`` are ``tile -> byte`` table callbacks; ``read_word`` backs the
    anim seeks (``8048``/``8058``)."""
    ty = (_s16(obj["y"]) >> 4) & 0xFF                        # [6993-6998]
    tx = (_s16(obj["x"]) >> 4) & 0xFF                        # [699A-699E]
    idx = (ty << 8) | tx
    tile_here = read_map(idx)                                # [69A4] map[(tx,ty)]
    tile_above = read_map((idx - 0x100) & 0xFFFF)            # [69A7] map[(tx,ty-1)]
    xv = _s16(obj["xvel"])                                   # [69AC-69B9]
    direction = 0 if xv == 0 else (1 if xv > 0 else -1)
    idx = (idx + direction) & 0xFFFF                         # [69BB]
    tile_ahead = read_map((idx - 0x100) & 0xFFFF)            # [69BD] map[(tx±1,ty-1)]
    a = prop_a(tile_ahead) & 0xFF                            # [69C2-69C5]
    flags = defn["d4"]
    if (flags & 0x40) and (flags & 0x80):                    # [69C9-69D3] both climb bits -> climbing
        if a != 0:                                           # [69D5] wall ahead-above -> back out X
            obj["x"] = (obj["x"] - (_s16(obj["xvel"]) >> 4)) & 0xFFFF   # [69D9-69DE]
        else:                                                # [69E3] reached the top -> dismount
            obj["anim_ptr"] = anim_script_rewind(obj["anim_ptr"], read_word)  # [69E3 8048]
            obj["yvel"] = 0                                  # [69E6]
            defn["d4"] = flags & 0x7F                        # [69EB] clear 0x80
            obj["y"] = (obj["y"] - 0x10) & 0xFFFF            # [69EF]
        return                                               # [69E0/69F3]
    if a != 0:                                               # [69F6] wall ahead
        if flags & 0x40:                                     # [69FA] start climbing
            obj["anim_ptr"] = anim_script_forward(obj["anim_ptr"], read_word)  # [6A00 8058]
            defn["d4"] = flags | 0x80                        # [6A03]
            obj["yvel"] = 0xFFF0                             # [6A07] climb up (-16)
            obj["x"] = (obj["x"] + (_s16(obj["xvel"]) >> 2)) & 0xFFFF   # [6A0C-6A13]
            return                                           # [6A15]
        obj["xvel"] = (-_s16(obj["xvel"])) & 0xFFFF          # [6A17] bounce off the wall
        obj["x"] = (obj["x"] + (_s16(obj["xvel"]) >> 4)) & 0xFFFF       # [6A1A-6A1F]
        # fall through to the vertical check
    obj["y"] = (obj["y"] - 0x10) & 0xFFFF                    # [6A24] probe one tile up
    if prop_b(tile_above) & 0xFF:                            # [6A2A-6A2D] solid above -> settle (Y still -0x10)
        _settle(obj, defn, tile_above, slope)                # [6A3E]
    else:
        obj["y"] = (obj["y"] + 0x10) & 0xFFFF                # [6A31] restore Y
        if (prop_b(tile_here) & 0xFF) == 0:                  # [6A35-6A3A] nothing underfoot -> fall
            if _s16(obj["yvel"]) < 0x100:                    # [6A6C] gravity, capped at 0x100
                obj["yvel"] = (obj["yvel"] + 0x10) & 0xFFFF  # [6A73]
        else:
            _settle(obj, defn, tile_here, slope)             # [6A3E]
