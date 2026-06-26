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
    "player_accel", "player_friction_dir", "player_friction_sym", "player_gravity",
    "player_set_anim", "player_advance_anim", "player_select_anim_id",
    "player_state_run", "player_state_anim5", "player_state_idle", "player_state_jump", "player_state_anim8",
    "player_state_anim4", "player_charge_6bce", "player_emit_trail", "JUMP_IMPULSE_TABLE",
    "X_MIN", "X_MAX", "VIEW_TILES", "TIMER_BYTES", "TIMER_WORD",
    "XVEL_FLOOR", "ANIM_SEQ_TABLE", "ANIM_ID_TABLE", "RUN_ACCEL_LIMIT",
    "TRAIL_RING_LO", "TRAIL_RING_HI", "TRAIL_STRIDE", "TRAIL_SPRITE",
]

TRAIL_RING_LO = 0x4F76    # [asm 5E31] lowest trail-ring slot; below it the ptr wraps to TRAIL_RING_HI
TRAIL_RING_HI = 0x4FBE    # [asm 5E37] wrap target (highest trail-ring slot)
TRAIL_STRIDE = 0x12       # [asm 5E2E] trail-ring slot stride
TRAIL_SPRITE = 0x35       # [asm 5E29] trail sprite id written into slot+4

RUN_ACCEL_LIMIT = 0x50    # [asm 5F03] the run state's horizontal speed cap passed to player_accel
JUMP_IMPULSE_TABLE = 0x79CE   # [asm 5F57] 9 words of per-frame Yvel impulse for the jump arc (decaying)
JUMP_FRAMES = 9               # [asm 5F50] frames driven by the impulse table before gravity takes over

ANIM_SEQ_TABLE = 0x7CDF   # [asm 6366/637D] base of the per-state animation-sequence pointer table
ANIM_ID_TABLE = 0x7B7F    # [asm 592E] base of the input-bitmask -> anim_id (FSM state index) table

XVEL_FLOOR = -0x60      # [asm 62FA] directional-friction floor on Xvel

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


def player_accel(xvel: int, facing: int, shift: int, input_held: bool, limit: int) -> int:
    """Recover the player horizontal accelerator ``1030:62B1``.

    When a left/right key is held (``[0x6BDB]``), add a facing-directed step ``sar(facing<<4, [0x4F24])`` to
    Xvel; then clamp the result to ``[-limit, +limit]`` (the per-state speed cap passed in ``bp``). ``facing``
    is the word ``[0x4F25]`` (+1 / -1). Pure: returns the new ``[0x4F22]``."""
    step = (_s16((facing << 4) & 0xFFFF) >> shift) if input_held else 0   # [62B9-62CF]
    dx = _s16((xvel + step) & 0xFFFF)                                     # [62D1-62D5]
    lim = _s16(limit & 0xFFFF)
    if dx >= lim:                                                         # [62D7] jge
        dx = lim
    elif dx <= -lim:                                                      # [62DB-62DF] neg; jle
        dx = -lim
    return dx & 0xFFFF


def player_friction_dir(xvel: int, force: int) -> int:
    """Recover the player directional friction ``1030:62EC``.

    Decay Xvel by ``force>>3`` (``force`` is the per-level constant ``[0x6BF6]``), floored at ``-0x60``."""
    nv = _s16((xvel - (force >> 3)) & 0xFFFF)                            # [62ED-62F6]
    if nv < XVEL_FLOOR:                                                  # [62FA] cmp,-0x60; jge
        nv = XVEL_FLOOR
    return nv & 0xFFFF


def player_friction_sym(xvel: int, shift: int) -> int:
    """Recover the player symmetric friction ``1030:6333``.

    Reduce the magnitude of Xvel by ``0xC>>shift`` (``shift`` = ``[0x4F24]``), clamped toward 0, preserving
    sign. Pure: returns the new ``[0x4F22]``."""
    a = _s16(xvel)                                                       # [6337-6340]
    neg = a < 0
    a = -a if neg else a
    a -= (0xC >> shift)                                                  # [6346-634B]
    if a < 0:                                                            # [634D] jae / xor
        a = 0
    return ((-a) & 0xFFFF) if neg else (a & 0xFFFF)                      # [6351-6357]


def player_gravity(yvel: int, water: int, limit: int) -> int:
    """Recover the player gravity ``1030:6309``.

    Add gravity to Yvel — ``0x10`` normally, ``4`` when the water flag ``[0x6BC7]==1`` (with the terminal
    velocity ``limit`` also divided by 8) — then cap at the terminal velocity. Pure: returns the new
    ``[0x4F2A]``."""
    grav = 0x10                                                         # [6310]
    term = _s16(limit & 0xFFFF)
    if water == 1:                                                      # [6313-6321]
        grav = 4
        term = term >> 3
    nv = _s16((yvel + grav) & 0xFFFF)                                   # [6323]
    if nv >= term:                                                     # [6325] jge -> cap
        nv = term
    return nv & 0xFFFF


def _sat_inc_byte(v: int) -> int:
    """``add v,1 ; sbb v,0`` — increment a byte counter, saturating at 0xFF (the counterpart of the timers'
    saturating *decrement*)."""
    v &= 0xFF
    return v if v == 0xFF else v + 1


def _inc_wrap_word(v: int) -> int:
    """``add v,1 ; adc v,0`` — increment a 16-bit counter; 0xFFFF wraps to 1 (the carry re-adds), not 0."""
    v &= 0xFFFF
    s = v + 1
    return ((s & 0xFFFF) + (1 if s > 0xFFFF else 0)) & 0xFFFF


def player_select_anim_id(bitmask: int, suppress: int, depth: int, anim_b_state: int, beb: int,
                          read_byte) -> tuple:
    """Recover the FSM state selection ``1030:5921..595C`` (the ``[0x6BC5]==0`` normal-play path).

    Map the 5-bit input ``bitmask`` to the player ``anim_id`` (the FSM state index used to dispatch
    ``cs:[anim_id*2 + 0x7D2F]``): ``anim_id = read_byte(0x7B7F + bitmask)``, forced to 0-bitmask when
    ``suppress`` (``[0x6BCD]``) is set, and overridden to 8 when ``depth`` (``[0x4F2D]``) >= 0x16. On an
    anim change (``anim_b_state`` ``[0x4F27]`` != anim_id) the run state resets (``[0x4F2C]``=0, ``[0x6BEB]``
    cleared); ``[0x6BEB]`` then increments (wrap-to-1). Pure: returns ``(anim_id, writes)`` where ``writes``
    maps ``{0x4F1B, 0x6BEB[, 0x4F2C]}`` to their new values (0x4F2C only on a change)."""
    bm = 0 if (suppress & 0xFF) != 0 else (bitmask & 0xFF)       # [5921-592C]
    anim_id = read_byte((bm + ANIM_ID_TABLE) & 0xFFFF) & 0xFF    # [592E]
    writes = {0x4F1B: depth & 0xFF}                              # [5932-5936] [0x4F1B]=[0x4F2D]
    if (depth & 0xFF) >= 0x16:                                   # [593A-593F]
        anim_id = 8
    if (anim_b_state & 0xFF) != anim_id:                         # [5941] anim changed -> reset run state
        beb = 0                                                  # [5947]
        writes[0x4F2C] = 0                                       # [594D]
    writes[0x6BEB] = _inc_wrap_word(beb)                         # [5952-5957]
    return anim_id, writes


def player_set_anim(anim_id: int, seq_index: int, cur_state: int, cur_ptr: int, read_word) -> tuple:
    """Recover the player animation-sequence selector ``1030:635D`` (== ``6374``, differing only in which
    state byte it tracks: ``635D`` uses ``[0x4F2C]``, ``6374`` uses ``[0x4F27]``).

    If the requested ``anim_id`` differs from the current state byte, switch: store ``anim_id`` and load a new
    sequence pointer ``[0x4F28] = read_word(seq_index + 0x7CDF)``. Otherwise keep the running pointer. Returns
    ``(new_state, new_ptr)`` — ``new_ptr`` is both the new ``[0x4F28]`` and the routine's returned ``bx`` (the
    composition feeds it straight into :func:`player_advance_anim`)."""
    anim_id &= 0xFF
    if (cur_state & 0xFF) != anim_id:                                # [635D/6374] cmp; jne
        return anim_id, read_word((seq_index + ANIM_SEQ_TABLE) & 0xFFFF)
    return cur_state & 0xFF, cur_ptr & 0xFFFF                        # [636F] unchanged -> bx = [0x4F28]


def player_advance_anim(anim_ptr: int, facing: int, read_word) -> tuple:
    """Recover the player animation stepper ``1030:638B``.

    Read the frame word at the sequence pointer; a negative word is a relative loop marker (rewind the pointer
    by it and re-read). The frame's high byte is stashed raw in ``[0x6BCF]``, then masked to 5 bits and OR'd
    with the facing sign bit (``[0x4F25]`` low byte ``& 0x80``) before the word is written to ``[0x4F20]``; the
    pointer advances by 2. Returns ``(frame_0x4F20, new_ptr_0x4F28, bcf_0x6BCF)``."""
    ax = read_word(anim_ptr & 0xFFFF)                               # [638E]
    if ax & 0x8000:                                                 # [6390] jns -> negative = loop marker
        anim_ptr = (anim_ptr + _s16(ax)) & 0xFFFF                   # [6394] bx += ax
        ax = read_word(anim_ptr)                                    # [6396] reload
    bcf = (ax >> 8) & 0xFF                                          # [6398] [0x6BCF] = high byte (raw)
    ah = (bcf & 0x1F) | (facing & 0x80)                            # [639C-63A6] mask + merge facing
    frame = ((ah << 8) | (ax & 0xFF)) & 0xFFFF                      # [63A8] [0x4F20] = ax
    new_ptr = (anim_ptr + 2) & 0xFFFF                              # [63AB-63AD] [0x4F28] += 2
    return frame, new_ptr, bcf


def player_state_run(fields: dict, read_word) -> dict:
    """Recover the ``anim_id==1`` "run" FSM handler ``1030:5EC4`` (the normal-play main path).

    The handler is a composition of the recovered primitives (the original source structure). With entry
    ``al==1`` (anim_id) and ``bx==2`` (anim_id*2 = the sequence index) preserved through the calls, the main
    path (gates ``[0x6BD0]==0`` no override, ``[0x6BC5]==0`` no scripted block) is::

        [0x6BD3] = sat_inc([0x6BD3])              # 5EF9 frame counter (caps at 0xFF)
        [0x4F22] = accel(limit=0x50)              # 5F03-5F06 player_accel
        [0x4F22] = friction_dir([0x4F22])         # 5F09 player_friction_dir
        ptr      = set_anim_b(anim=1, seq=2)      # 5F0C player_set_anim ([0x4F27]/[0x4F28])
        advance_anim(ptr)                         # 5F0F player_advance_anim ([0x4F20]/[0x4F28]/[0x6BCF])

    ``fields`` supplies the initial player words/bytes it reads; returns the dict of writes. Pure."""
    out = {}
    out[0x6BD3] = _sat_inc_byte(fields[0x6BD3])                                          # [5EF9-5EFE]
    xvel = player_accel(fields[0x4F22], fields[0x4F25], fields[0x4F24],                  # [5F03-5F06]
                        fields[0x6BDB] != 0, RUN_ACCEL_LIMIT)
    xvel = player_friction_dir(xvel, fields[0x6BF6])                                     # [5F09]
    out[0x4F22] = xvel
    state, ptr = player_set_anim(1, 2, fields[0x4F27], fields[0x4F28], read_word)        # [5F0C] set_anim_b
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, fields[0x4F25] & 0xFF, read_word)     # [5F0F]
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf
    return out


def player_emit_trail(player_x: int, player_y: int, blink: int, ring_ptr: int):
    """Recover the player trail-sprite emitter ``1030:5E11`` (called from the moving-idle path).

    Gated to every 4th frame (``[0x6BD5] & 3 == 0``): push a sprite ``(x, y, id=0x35)`` into the ring buffer at
    ``[0x6BBE]`` then step the pointer back by 0x12, wrapping below ``0x4F76`` to ``0x4FBE``. Returns ``None``
    when gated, else ``(word_writes, new_ring_ptr)`` where ``word_writes`` maps ring offsets to 16-bit values."""
    if blink & 3:                                               # [5E11-5E16] gated
        return None
    bx = ring_ptr & 0xFFFF
    writes = {bx: player_x & 0xFFFF,                            # [5E1E-5E21] slot+0 = X
              (bx + 2) & 0xFFFF: player_y & 0xFFFF,             # [5E23-5E26] slot+2 = Y
              (bx + 4) & 0xFFFF: TRAIL_SPRITE}                  # [5E29] slot+4 = 0x35
    bx = (bx - TRAIL_STRIDE) & 0xFFFF                           # [5E2E]
    if bx < TRAIL_RING_LO:                                      # [5E31-5E37]
        bx = TRAIL_RING_HI
    return writes, bx


def player_charge_6bce(v: int) -> int:
    """Recover the small shared helper ``1030:5EB7`` — grow the ``[0x6BCE]`` counter by 2 while it is <= 0x30
    (used by the anim_id 4 & 5 handlers; ``[0x6BCE]`` is also one of the per-frame timers)."""
    v &= 0xFF
    return (v + 2) & 0xFF if v <= 0x30 else v


def player_state_anim5(fields: dict, read_word) -> dict:
    """Recover the ``anim_id==5`` FSM handler ``1030:5E96`` (main path, gate ``[0x6BD0]==0``).

    A clean composition (entry ``al==5``, ``bx==0x0A`` preserved into ``set_anim_b``)::

        [0x6BC8]=0 ; [0x6BE1]=4                      # 5EA0/5EA5
        ptr = set_anim_b(anim=5, seq=0x0A)           # 5EAA player_set_anim ([0x4F27]/[0x4F28])
        advance_anim(ptr)                            # 5EAD player_advance_anim ([0x4F20]/[0x4F28]/[0x6BCF])
        [0x4F22] = friction_sym([0x4F22])            # 5EB0 player_friction_sym
        [0x6BCE] = charge_6bce([0x6BCE])             # 5EB3 -> 5EB7

    ``fields`` supplies the initial words/bytes; returns the dict of writes. Pure."""
    out = {0x6BC8: 0, 0x6BE1: 4}
    state, ptr = player_set_anim(5, 0x0A, fields[0x4F27], fields[0x4F28], read_word)      # [5EAA]
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, fields[0x4F25] & 0xFF, read_word)      # [5EAD]
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf
    out[0x4F22] = player_friction_sym(fields[0x4F22], fields[0x4F24])                     # [5EB0]
    out[0x6BCE] = player_charge_6bce(fields[0x6BCE])                                      # [5EB3->5EB7]
    return out


def _idle_set_advance(out: dict, anim_id: int, seq: int, rb, rw, facing: int) -> None:
    """Idle helper: ``set_anim_a`` (635D, tracks ``[0x4F2C]``) then ``advance_anim`` (638B)."""
    state, ptr = player_set_anim(anim_id, seq, rb(0x4F2C), rw(0x4F28), rw)
    out[0x4F2C] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, facing & 0xFF, rw)
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf


def _idle_default_anim(out: dict, entry_bx: int, facing: int, rw) -> None:
    """Idle "default" anim path ``1030:5DED`` — load the sequence for the handler's entry ``bx`` (anim_id*2;
    0 for a direct idle, but e.g. 4 when the jump handler falls through) and write frame 0 WITHOUT advancing
    and WITHOUT the 0x1F mask (only the facing bit is merged); resets ``[0x4F2C]``."""
    ptr = rw((entry_bx + ANIM_SEQ_TABLE) & 0xFFFF)              # [5DED] bx = [entry_bx + 0x7CDF]
    out[0x4F28] = ptr                                            # [5DF1]
    ax = rw(ptr)                                                 # [5DF5]
    ah = ((ax >> 8) | (facing & 0x80)) & 0xFF                    # [5DF7-5DFE] no 0x1F mask here
    out[0x4F20] = ((ah << 8) | (ax & 0xFF)) & 0xFFFF            # [5E00]
    out[0x4F2C] = 0                                              # [5E03]


def player_state_idle(rb, rw, entry_bx: int = 0) -> dict:
    """Recover the ``anim_id==0`` "idle" FSM handler ``1030:5CDB`` (main path, gate ``[0x6BD0]==0``).

    The grounded idle/landing/turn/fidget state. ``rb``/``rw`` read entry memory (byte/word); ``entry_bx`` is
    the dispatch ``bx`` (anim_id*2) — 0 for a direct idle, but other handlers fall through here with their own
    bx (e.g. the jump handler with 4), which only affects the 5DED default-anim sequence. Returns the dict of
    writes. The witnessed paths (see docs/pre2/player_fsm_island.md): airborne, moving+turn (anim 0x12 +
    trail), default (5DED), long-idle (anim 0x10), and fidget (anim 0x11 via the table at 0x79E0). The
    short-idle anim-0x13 path + dust effects (3435/3414) never fire and are left unrecovered (fail loud)."""
    out = {0x6BC8: 0}                                            # [5CE8]
    xv = player_friction_dir(rw(0x4F22), rw(0x6BF6))            # [5CED]
    xv = player_friction_sym(xv, rb(0x4F24))                    # [5CF0]
    out[0x4F22] = xv

    if rb(0x6BFE) == 0 and rw(0x4F2A) != 0:                     # [5CF3-5CFF] airborne (in the air)
        if rb(0x6BD1) > 4:                                      # [5D01] jbe
            out[0x4F22] = player_friction_dir(out[0x4F22], rw(0x6BF6))   # [5D08]
        return out                                              # [5D0B] jmp 5E0D (no [0x4F27] reset)

    facing = rb(0x4F25)
    ax = abs(_s16(out[0x4F22]))                                 # [5D0E-5D15] |Xvel| (post-friction)
    if ax >= 8:                                                 # [5D17] jb 5D42
        if ((rb(0x4F21) >> 7) & 1) == ((out[0x4F22] >> 15) & 1):   # [5D1C-5D2C] facing == vel sign?
            _idle_set_advance(out, 0x12, 0x24, rb, rw, facing)      # [5D31-5D39] anim 0x12
            trail = player_emit_trail(rw(0x4F1C), rw(0x4F1E), rb(0x6BD5), rw(0x6BBE))  # [5D3C] call 5E11
            if trail is not None:
                out.update(trail[0])
                out[0x6BBE] = trail[1]
        else:
            _idle_default_anim(out, entry_bx, facing, rw)                # [5D2E] jmp 5DED
        out[0x4F27] = 0                                         # [5E08]
        return out

    if _s16(out[0x4F22]) != 0:                                  # [5D42-5D46] 0 < |Xvel| < 8 -> default
        _idle_default_anim(out, entry_bx, facing, rw)
        out[0x4F27] = 0
        return out

    # Xvel == 0 [5D49]
    timer = rb(0x6BD3)
    e9 = rb(0x27E9)
    eced = rb(0x27EC) & rb(0x27ED)
    if timer >= 0x1E:                                           # [5D49] jb 5D73
        if e9 == 0 and eced == 0:                               # [5D50-5D5E] no input -> long idle
            out[0x6BD3] = (timer - 3) & 0xFF                    # [5D60]
            _idle_set_advance(out, 0x10, 0x20, rb, rw, facing)  # [5D65-5D6D] anim 0x10
            out[0x4F27] = 0
            return out
        reach_5d83 = True                                      # input present -> 5D83
    else:                                                       # [5D73]
        if eced != 0:
            reach_5d83 = True
        elif e9 == 0:
            reach_5d83 = False                                 # [5D7C] je 5DC9 (fidget)
        else:
            reach_5d83 = True

    if reach_5d83 and rb(0x6BFE) == 0:                          # [5D83] jne 5DC9 ; else 5D8A
        raise NotImplementedError("idle anim-0x13 path (5D8A) is unwitnessed/unrecovered")

    # fidget [5DC9]: find the 0x79E0 range [lo,hi) containing key=[0x27F0]&0x1FF -> anim 0x11; below lo -> default
    key = rw(0x27F0) & 0x1FF
    si = 0x79E0
    while True:
        if key < rw(si):                                       # [5DD2] jb 5DED
            _idle_default_anim(out, entry_bx, facing, rw)
            out[0x4F27] = 0
            return out
        if key < rw((si + 2) & 0xFFFF):                        # [5DD6] jb 5DE0
            _idle_set_advance(out, 0x11, 0x22, rb, rw, facing)  # [5DE0-5DE8] anim 0x11
            out[0x4F27] = 0
            return out
        si = (si + 4) & 0xFFFF                                 # [5DDB]


def player_state_jump(rb, rw) -> dict:
    """Recover the ``anim_id==2`` "jump/rising" FSM handler ``1030:5F30`` (main path, gate ``[0x6BD0]==0``).

    Falls through to the idle handler when ``[0x6BE0]!=0`` (entering with ``bx==4``). Otherwise: drive the jump
    arc — for the first ``9`` frames add the decaying impulse ``[0x79CE + counter*2]`` to Yvel (counter is
    ``[0x6BD1]``, post-incremented), then switch to gravity; apply horizontal control (accel toward 0x30 when
    Xvel is small, else symmetric friction); ``set_anim_b(2, seq=4)`` + advance; finally two directional
    frictions. ``rb``/``rw`` read entry memory; returns the dict of writes."""
    if rb(0x6BE0) != 0:                                          # [5F37-5F3E] jmp 5CDB
        return player_state_idle(rb, rw, entry_bx=4)
    out = {0x6BFE: 0}                                            # [5F41]
    counter = rb(0x6BD1)                                         # [5F46]
    out[0x6BD1] = (counter + 1) & 0xFF                          # [5F4C] inc
    if counter < JUMP_FRAMES:                                    # [5F50] jae
        impulse = rw((JUMP_IMPULSE_TABLE + counter * 2) & 0xFFFF)   # [5F55-5F57] (no scripted halving: [0x6BC5]==0)
        out[0x4F2A] = (rw(0x4F2A) + impulse) & 0xFFFF           # [5F64] Yvel += impulse
    else:
        out[0x4F2A] = player_gravity(rw(0x4F2A), rb(0x6BC7), 0xC0)   # [5F6A-5F6D] gravity

    xvel = rw(0x4F22)
    if (xvel & 0xFFFF) < 0x30:                                  # [5F73] jb (unsigned)
        xvel = player_accel(xvel, rw(0x4F25), rb(0x4F24), rb(0x6BDB) != 0, 0x30)   # [5F7E]
    else:
        xvel = player_friction_sym(xvel, rb(0x4F24))            # [5F79]

    state, ptr = player_set_anim(2, 4, rb(0x4F27), rw(0x4F28), rw)   # [5F81-5F86] set_anim_b
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, rb(0x4F25) & 0xFF, rw)   # [5F89]
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf

    xvel = player_friction_dir(xvel, rw(0x6BF6))               # [5F8C]
    xvel = player_friction_dir(xvel, rw(0x6BF6))               # [5F8F]
    out[0x4F22] = xvel
    return out


def player_state_anim8(rb, rw) -> dict:
    """Recover the ``anim_id==8`` FSM handler ``1030:5CCE`` (the depth-override state; no ``[0x6BD0]`` gate).

    ``friction_dir; friction_sym; set_anim_b; advance_anim``. NOTE the register-flow gotcha: ``friction_sym``
    (6333) leaves ``ax`` = the new Xvel, so the following ``set_anim_b`` (6374) is called with ``al`` = that
    Xvel's low byte (NOT the anim_id) and ``bx`` == 0x10 (anim_id*2) as the sequence index. Faithful to the
    ASM. ``rb``/``rw`` read entry memory; returns the dict of writes."""
    xv = player_friction_dir(rw(0x4F22), rw(0x6BF6))           # [5CCE]
    xv = player_friction_sym(xv, rb(0x4F24))                   # [5CD1] -> ax = xv
    out = {0x4F22: xv}
    state, ptr = player_set_anim(xv & 0xFF, 0x10, rb(0x4F27), rw(0x4F28), rw)   # [5CD4] al = xv low byte
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, rb(0x4F25) & 0xFF, rw)       # [5CD7]
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf
    return out


def player_state_anim4(rb, rw) -> dict:
    """Recover the ``anim_id==4`` FSM handler ``1030:5E62`` (main path, gate ``[0x6BD0]==0``).

    Always: ``[0x6BD3]=0``, ``[0x6BE1]=4``, ``charge_6bce``. Then on ``|Xvel| <= 0x20`` accelerate (limit 0x20)
    + ``set_anim_b`` + advance (``al`` = the clobbered ``|Xvel|`` low byte, ``bx``==8); otherwise fall through
    to the idle handler (bx==8), which — because ``[0x6BD3]`` was just zeroed — sees a fresh idle timer."""
    out = {0x6BD3: 0, 0x6BE1: 4, 0x6BCE: player_charge_6bce(rb(0x6BCE))}   # [5E6C-5E76]
    mag = abs(_s16(rw(0x4F22)))                                            # [5E79]
    if mag <= 0x20:                                                        # [5E85-5E87] jbe -> accel
        out[0x4F22] = player_accel(rw(0x4F22), rw(0x4F25), rb(0x4F24), rb(0x6BDB) != 0, 0x20)  # [5E8C]
        state, ptr = player_set_anim(mag & 0xFF, 8, rb(0x4F27), rw(0x4F28), rw)   # [5E8F] al = clobbered |Xvel|
        out[0x4F27] = state
        frame, new_ptr, bcf = player_advance_anim(ptr, rb(0x4F25) & 0xFF, rw)     # [5E92]
        out[0x4F28] = new_ptr
        out[0x4F20] = frame
        out[0x6BCF] = bcf
        return out
    # [5E89] jmp 5CDB — idle sees [0x6BD3]==0 (just written) and bx==8
    rb2 = lambda o: 0 if o == 0x6BD3 else rb(o)
    out.update(player_state_idle(rb2, rw, entry_bx=8))
    return out


def player_tick_timers(timers: dict) -> dict:
    """Recover the player-update timer tail ``1030:5A47..5A87``.

    Decrement each of the seven byte countdown timers + the one word countdown timer, every one clamped at 0.
    Pure: ``timers`` maps each address in ``TIMER_BYTES``/``TIMER_WORD`` to its current value; returns the new
    values (same keys). Bytes are 8-bit-wrapped, the word is 16-bit."""
    out = {a: _dec_floor(timers[a], 8) for a in TIMER_BYTES}     # [5A4A-5A7E] seven byte timers
    out[TIMER_WORD] = _dec_floor(timers[TIMER_WORD], 16)         # [5A82-5A87] one word timer
    return out
