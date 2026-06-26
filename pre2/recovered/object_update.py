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
           "FAR_X", "FAR_Y", "EMPTY_ID", "DespawnResult", "despawn_check", "on_screen_tile",
           "anim_script_rewind", "anim_script_forward", "despawn_full", "dying_state", "saturating_counter",
           "handle_object_7665", "handle_object_773d", "handle_object_77de", "handle_object_7c8c"]

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

    ``obj``: x, y, id, xvel, yvel, state. ``defn``: d4, dD (left bound, word), dF (right bound, word),
    d11 (signed-byte speed, mutated), d12 (speed magnitude limit). ``glb``: player_x, player_y."""
    drawn = obj["id"] & 0x2000
    st = obj["state"]
    if not drawn and st != 0xFF:                             # [7740-7784] proximity despawn (skip if drawn/dying)
        if _abs16(obj["y"] - glb["player_y"]) >= 0xBE:       # [774C-775A] too far vertically -> despawn
            keep = False
        else:
            px, dD, dF = _s16(glb["player_x"]), _s16(defn["dD"]), _s16(defn["dF"])
            keep = (dF + 0x1E0 > px) if px >= dD else (px + 0x1E0 >= dD)   # [775C-7779] horizontal window
        if not keep:
            obj["id"] = EMPTY_ID                             # [777B] despawn ([si+4]=0xFFFF, [def+4]&=0xFB)
            defn["d4"] &= 0xFB
            return

    if st == 0:                                              # [7785] patrol right: accelerate +3 up to +d12
        sp = _s8(defn["d11"])
        obj["xvel"] = sp & 0xFFFF                            # [7790] Xvel = current speed
        if sp + 3 <= (defn["d12"] & 0xFF):                   # [7793-779D] cap at [def+0x12]
            defn["d11"] = (sp + 3) & 0xFF
        if _s16(defn["dF"]) < _s16(obj["x"]):                # [77A2-77A9] past the right bound -> turn
            obj["state"] = 1
    elif st == 1:                                            # [77AE] patrol left: accelerate -3 down to -d12
        sp = _s8(defn["d11"])
        obj["xvel"] = sp & 0xFFFF
        if sp - 3 >= -(defn["d12"] & 0xFF):                  # [77B9-77C5] cap at -[def+0x12]
            defn["d11"] = (sp - 3) & 0xFF
        if _s16(defn["dD"]) >= _s16(obj["x"]):               # [77CA-77D1] past the left bound -> turn
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
