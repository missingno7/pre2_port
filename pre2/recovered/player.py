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
    "player_state_anim4", "player_state_attack", "player_dispatch_handler", "PLAYER_HANDLERS",
    "player_fsm_frontend", "player_fsm_step", "FSM_WORD_FIELDS",
    "player_charge_6bce", "player_emit_trail", "JUMP_IMPULSE_TABLE", "ATTACK_PHASE_TABLE",
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
ATTACK_PHASE_TABLE = 0x7B04      # [asm 5FAF/6081] 5-byte per-phase records {frametbl_ptr w, sfx b, v19 b, flag b}
ATTACK_SPAWN_LIST = 0x4F2E       # [asm 627D] the projectile list the attack handler spawns into (4 slots, 0x12)

# 16-bit-word DS fields written by player_fsm_step; every other write in its contract is a byte. Used by the
# live FSM checkpoint to apply / diff each write at the right width (the byte-vs-word lesson from collision).
FSM_WORD_FIELDS = frozenset(
    {0x4F0A, 0x4F0C, 0x4F0E, 0x4F20, 0x4F22, 0x4F25, 0x4F28, 0x4F2A, 0x6BBE, 0x6BEB}
    | {s + d for s in range(ATTACK_SPAWN_LIST, ATTACK_SPAWN_LIST + 4 * 0x12, 0x12)  # projectile slots:
       for d in (0, 2, 4, 6, 0xC, 0xE)}                                            # words (the +8 field is a byte)
    | set(range(TRAIL_RING_LO, 0x4FC2, 2))                                         # trail/dust ring slots
)

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


def player_state_run(rb, rw) -> dict:
    """Recover the ``anim_id==1`` "run" FSM handler ``1030:5EC4`` (the normal-play main path).

    The handler is a composition of the recovered primitives (the original source structure). With entry
    ``al==1`` (anim_id) and ``bx==2`` (anim_id*2 = the sequence index) preserved through the calls, the main
    path (gates ``[0x6BD0]==0`` no override, ``[0x6BC5]==0`` no scripted block) is::

        [0x6BD3] = sat_inc([0x6BD3])              # 5EF9 frame counter (caps at 0xFF)
        [0x4F22] = accel(limit=0x50)              # 5F03-5F06 player_accel
        [0x4F22] = friction_dir([0x4F22])         # 5F09 player_friction_dir
        ptr      = set_anim_b(anim=1, seq=2)      # 5F0C player_set_anim ([0x4F27]/[0x4F28])
        advance_anim(ptr)                         # 5F0F player_advance_anim ([0x4F20]/[0x4F28]/[0x6BCF])

    ``rb``/``rw`` read entry memory; returns the dict of writes. Pure."""
    out = {}
    out[0x6BD3] = _sat_inc_byte(rb(0x6BD3))                                              # [5EF9-5EFE]
    xvel = player_accel(rw(0x4F22), rw(0x4F25), rb(0x4F24), rb(0x6BDB) != 0, RUN_ACCEL_LIMIT)  # [5F03-5F06]
    xvel = player_friction_dir(xvel, rw(0x6BF6))                                         # [5F09]
    out[0x4F22] = xvel
    state, ptr = player_set_anim(1, 2, rb(0x4F27), rw(0x4F28), rw)                       # [5F0C] set_anim_b
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, rb(0x4F25) & 0xFF, rw)                # [5F0F]
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


def player_state_anim5(rb, rw) -> dict:
    """Recover the ``anim_id==5`` FSM handler ``1030:5E96`` (main path, gate ``[0x6BD0]==0``).

    A clean composition (entry ``al==5``, ``bx==0x0A`` preserved into ``set_anim_b``)::

        [0x6BC8]=0 ; [0x6BE1]=4                      # 5EA0/5EA5
        ptr = set_anim_b(anim=5, seq=0x0A)           # 5EAA player_set_anim ([0x4F27]/[0x4F28])
        advance_anim(ptr)                            # 5EAD player_advance_anim ([0x4F20]/[0x4F28]/[0x6BCF])
        [0x4F22] = friction_sym([0x4F22])            # 5EB0 player_friction_sym
        [0x6BCE] = charge_6bce([0x6BCE])             # 5EB3 -> 5EB7

    ``rb``/``rw`` read entry memory; returns the dict of writes. Pure."""
    out = {0x6BC8: 0, 0x6BE1: 4}
    state, ptr = player_set_anim(5, 0x0A, rb(0x4F27), rw(0x4F28), rw)                     # [5EAA]
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, rb(0x4F25) & 0xFF, rw)                 # [5EAD]
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf
    out[0x4F22] = player_friction_sym(rw(0x4F22), rb(0x4F24))                             # [5EB0]
    out[0x6BCE] = player_charge_6bce(rb(0x6BCE))                                          # [5EB3->5EB7]
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
    short-idle look-around anim-0x13 path (5D8A) — set_anim(0x13)+advance then the camera-pan effect
    (3435/3414 -> the scroll/render sub-island 3588/350c) — is NOT recovered and fails loud. It does fire under
    the live-collapse trajectory (idle look-around), so it gates the FSM *live-drive*; recovering it (the camera
    pan) is the next step. The verify oracle is unaffected (28/28 demos clean)."""
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


# The per-anim_id FSM handler table (the recovered counterpart of ``cs:[anim_id*2 + 0x7D2F]``). anim_ids 3/6/7
# share the audio-coupled "attack" (door-bash/secret-reveal) handler 0x5F96 (not yet recovered).
PLAYER_HANDLERS = {
    0: player_state_idle,     # 0x5CDB
    1: player_state_run,      # 0x5EC4
    2: player_state_jump,     # 0x5F30
    4: player_state_anim4,    # 0x5E62
    5: player_state_anim5,    # 0x5E96
    8: player_state_anim8,    # 0x5CCE
}


def player_fsm_frontend(rb, rw) -> tuple:
    """Recover the FSM front-end ``1030:58A7..591F`` (after the input-decode call ``DC1``).

    Combine the six decoded input flags (``[0x27E8..0x27ED]``) into ``[0x6BDB]``/``[0x6BDC]``, update the
    facing word ``[0x4F25]`` (+/-1; resetting ``[0x6BEB]`` on a turn), and pack the 5-bit dispatch bitmask from
    bit0 of ``[0x27EC],[0x27ED],[0x27EA],[0x27EB],[0x27E8]``. Returns ``(bitmask, writes)`` where ``writes`` may
    include ``[0x6BDB],[0x6BDC],[0x4F25],[0x6BEB]``. Pure."""
    ec, ed = rb(0x27EC), rb(0x27ED)
    ea, eb, e8 = rb(0x27EA), rb(0x27EB), rb(0x27E8)
    writes = {0x6BDB: ed | ec, 0x6BDC: ea | eb}                  # [58A7-58BB]
    facing = rw(0x4F25)                                          # [58BF-58FC] facing update
    if ec != 0:
        if ed == 0 and (facing & 0xFFFF) != 1:
            writes[0x4F25] = 1
            writes[0x6BEB] = 0
    elif ed != 0:
        if (facing & 0xFFFF) != 0xFFFF:
            writes[0x4F25] = 0xFFFF
            writes[0x6BEB] = 0
    bitmask = 0                                                  # [58FC-591F] pack bit0 of the 5 flags
    for flag in (ec, ed, ea, eb, e8):
        bitmask = ((bitmask << 1) | (flag & 1)) & 0xFF
    return bitmask, writes


def player_fsm_step(rb, rw) -> tuple:
    """Compose the full per-frame player FSM ``1030:58A7..5A0B`` (the ``[0x6BC5]==0`` normal-play path):
    front-end -> ``select_anim_id`` -> dispatch to the recovered handler. Returns ``(writes, sfx)``.

    Threads the intermediate writes the way the ASM does: the facing/state changes from the front-end and the
    ``[0x4F2C]`` reset from the selector are visible to the handler (it reads ``[0x4F25]``/``[0x4F2C]``)."""
    bitmask, writes = player_fsm_frontend(rb, rw)               # [58A7-591F]
    beb = writes.get(0x6BEB, rw(0x6BEB))
    anim_id, sel_writes = player_select_anim_id(bitmask, rb(0x6BCD), rb(0x4F2D),  # [5921-595C]
                                                rb(0x4F27), beb, rb)
    writes.update(sel_writes)                                   # [0x4F1B], [0x6BEB], maybe [0x4F2C]

    # The handler reads back fields the front-end/selector just wrote — facing [0x4F25], the [0x4F2C] reset,
    # and crucially the input-held flags [0x6BDB]/[0x6BDC] that drive player_accel. Expose every pending write
    # through a read overlay (the ASM reads them from memory mid-routine).
    def rb2(off):
        return (writes[off] & 0xFF) if off in writes else rb(off)

    def rw2(off):
        return (writes[off] & 0xFFFF) if off in writes else rw(off)

    # Handlers for anim_id 0/1/2/4/5 begin with `cmp [0x6BD0],0 ; jne 5F93` — when the override flag is set they
    # run the shared override tail 0x5F93 (== the attack body with al=[0x4F27]). The attack handler itself
    # (anim_id 3/6/7 = 5F96, the tail's own body) has no such prologue — it can't override into itself — and
    # anim_id 8 (5CCE) has none either; both dispatch normally.
    if rb(0x6BD0) != 0 and anim_id not in (3, 6, 7, 8):         # [5F35-etc] -> 5F93 override
        hw, sfx = player_state_attack(rb2(0x4F27), anim_id * 2, rb2, rw2)
    else:
        hw, sfx = player_dispatch_handler(anim_id, rb2, rw2)    # [5A0B] call cs:[anim_id*2 + 0x7D2F]
    writes.update(hw)
    return writes, sfx


def _attack_render_sprite(out: dict, rec: int, frame: int, rb, rw) -> None:
    """1030:6081 — map the current anim frame to the player render sprite via the phase's frame table (8-byte
    records {frame, sprite_id, x_off, y_off}, 0x55AA terminator). Sets [0x4F0E]/[0x4F0A]/[0x4F0C] with the
    facing flip; leaves them unchanged if the frame is not in the table."""
    base = rw(rec)                                              # [6081] si = phase.frametbl_ptr
    dh = (frame >> 8) & 0x80                                    # [6088-60A5] facing bit of the frame
    want = frame & 0x1FFF                                       # [6085-608A] frame, high byte masked to 0x1F
    off = 0xFFF8
    while True:
        off = (off + 8) & 0xFFFF                                # [6090]
        cx = rw((off + base) & 0xFFFF)                          # [6093]
        if cx == 0x55AA:                                        # [6095] terminator -> not found, leave sprite
            return
        if cx == want:                                          # [609B-609D]
            break
    sprid = rw((off + base + 2) & 0xFFFF)                       # [609F]
    cx = rw((off + base + 4) & 0xFFFF)                          # [60A2] x offset
    yoff = rw((off + base + 6) & 0xFFFF)                        # [60D8]
    if dh:                                                      # [60A5-60AC] facing flip
        sprid |= dh << 8
        cx = (-_s16(cx)) & 0xFFFF
    out[0x4F0E] = sprid & 0xFFFF                                # [60AE]
    out[0x4F0A] = (rw(0x4F1C) + (_s16(rw(0x4F22)) >> 4) - _s16(cx)) & 0xFFFF    # [60B1-60C4]
    out[0x4F0C] = (rw(0x4F1E) + (_s16(rw(0x4F2A)) >> 4) - _s16(yoff)) & 0xFFFF  # [60C7-60DB]


def _attack_spawn(out: dict, rec: int, rb, rw) -> bool:
    """1030:6017-6070 — spawn a projectile into the first free 0x4F2E slot (stride 0x12, 4 slots; free = [+4]
    ==0xFFFF). Reads the projectile's sprite/offsets from just past the phase frame-table's terminator.
    Returns True if a slot was taken (the caller then sets [0x4F0E]=0xFFFF)."""
    si = None                                                  # [627C-6293] find a free 0x4F2E slot
    p = ATTACK_SPAWN_LIST
    for _ in range(4):
        if rw((p + 4) & 0xFFFF) == 0xFFFF:
            si = p
            break
        p = (p + 0x12) & 0xFFFF
    if si is None:
        return False
    out[(si + 8) & 0xFFFF] = (rb((rec + 4) & 0xFFFF) >> 1) & 3  # [601C-601E] [si+8] = (flag>>1)&3 (al is post-shr)
    bx = rw(rec)                                               # [6021] frame-table ptr
    while rw(bx) != 0x55AA:                                    # [6025-602B] walk to the terminator
        bx = (bx + 2) & 0xFFFF
    bx = (bx + 6) & 0xFFFF                                     # [602D] past terminator -> the spawn record
    out[(si + 0xC) & 0xFFFF] = bx                              # [6030]
    sprid = rw(bx)                                             # [6033] ax
    cx = rw((bx - 4) & 0xFFFF)                                 # [6035] x offset
    yoff = rw((bx - 2) & 0xFFFF)                               # [6038]
    out[(si + 0xE) & 0xFFFF] = yoff                            # [603B]
    if rb(0x4F25) & 0x80:                                      # [603E-6049] facing flip
        sprid |= 0x8000
        cx = (-_s16(cx)) & 0xFFFF
    out[(si + 4) & 0xFFFF] = sprid & 0xFFFF                    # [604B]
    out[(si + 6) & 0xFFFF] = cx & 0xFFFF                       # [604E]
    out[si] = (rw(0x4F0A) + (_s16(cx) >> 4)) & 0xFFFF          # [6051-605E] pos relative to the render sprite
    out[(si + 2) & 0xFFFF] = (rw(0x4F0C) + (_s16(yoff) >> 4)) & 0xFFFF  # [6060-606D]
    return True


def player_state_attack(al: int, bx: int, rb, rw) -> tuple:
    """Recover the ``anim_id 3/6/7`` "attack" FSM handler ``1030:5F96`` AND the shared override tail ``5F93``
    (same code; ``5F93`` just sets ``al=[0x4F27]`` first). This is the caveman's club-bash action — witnessed
    driving the door-bashing (`demo …015934`) and secret-tile reveal (`…015822`); the spawned `0x4F2E`
    projectile is the bash hitbox. ``al``=entry anim id / state, ``bx``=anim_id*2 (the set_anim sequence
    index). Returns ``(writes, sfx)`` where ``sfx`` is a list of ``play_sfx`` dl values.

    Common: set_anim_b + advance + friction_sym + sat_inc[0x6BD3]; ``[0x7B19]`` = phase.v19 (x4 if [0x6BCE]);
    ``[0x6BD0]`` = (~[0x6BCF])&0x40 (the advance_anim high byte's bit6) — which selects the branch:
    bit6 clear -> the 6081 render-sprite path; bit6 set -> the sound path (play_sfx, trail, Yvel nudge, spawn)."""
    out: dict = {}
    sfx: list = []
    state, ptr = player_set_anim(al, bx, rb(0x4F27), rw(0x4F28), rw)          # [5F96]
    out[0x4F27] = state
    frame, new_ptr, bcf = player_advance_anim(ptr, rb(0x4F25) & 0xFF, rw)     # [5F99]
    out[0x4F28] = new_ptr
    out[0x4F20] = frame
    out[0x6BCF] = bcf
    out[0x4F22] = player_friction_sym(rw(0x4F22), rb(0x4F24))                 # [5F9C]
    out[0x6BD3] = _sat_inc_byte(rb(0x6BD3))                                   # [5F9F]

    phase = rb(0x7B18)                                                        # [5FA9-5FAF]
    rec = (ATTACK_PHASE_TABLE + 5 * phase) & 0xFFFF
    v19 = rb((rec + 3) & 0xFFFF)                                              # [5FB1] phase.v19
    if rb(0x6BCE) != 0:                                                       # [5FB5-5FBE]
        v19 = (v19 << 2) & 0xFF
    out[0x7B19] = v19                                                         # [5FC0]
    bd0 = (~bcf) & 0x40                                                       # [5FC3-5FC8]
    out[0x6BD0] = bd0

    # The render-sprite/spawn read [0x4F22]/[0x4F2A] *after* this routine's friction (and the sound path's Yvel
    # nudge) wrote them — expose pending writes through an overlay.
    def rb_ov(off):
        return (out[off] & 0xFF) if off in out else rb(off)

    def rw_ov(off):
        return (out[off] & 0xFFFF) if off in out else rw(off)

    if bd0 != 0:                                                              # [5FCD-5FCF] -> 6081 main
        _attack_render_sprite(out, rec, frame, rb_ov, rw_ov)
        return out, sfx

    # sound path [5FD2+]
    out[0x6BCD] = rb((rec + 2) & 0xFFFF)                                      # [5FD2] phase.sfx
    sfx.append(5 if phase == 0 else (0 if phase == 1 else 0x0A))             # [5FD9-5FEB] play_sfx dl
    a27 = out[0x4F27]                                                         # [5FF0] (post set_anim)
    if a27 == 6:                                                             # [5FF3-5FF5]
        dy = 0
    elif a27 == 3:                                                           # [5FF7/5FFA-5FFC] dx=0xFFE0
        dy = (-0x20) & 0xFFFF
    else:                                                                    # [5FFE] dx=0xFFD0
        dy = (-0x30) & 0xFFFF
        trail = player_emit_trail(rw(0x4F1C), rw(0x4F1E), rb(0x6BD5), rw(0x6BBE))  # [6001] call 5E11
        if trail is not None:
            out.update(trail[0])
            out[0x6BBE] = trail[1]
    if rb(0x6BFE) == 0:                                                       # [6004-600B]
        out[0x4F2A] = (rw(0x4F2A) + dy) & 0xFFFF
    if (rb((rec + 4) & 0xFFFF) & 1) and _attack_spawn(out, rec, rb_ov, rw_ov):  # [600F-6017] flag bit0 + free slot
        out[0x4F0E] = 0xFFFF                                                  # [6070]
    elif rb(0x6BD2) == 0:                                                     # [6075-607A]
        _attack_render_sprite(out, rec, frame, rb_ov, rw_ov)                  # [6081]
    else:
        out[0x4F0E] = 0xFFFF                                                  # [607C]
    return out, sfx


def player_dispatch_handler(anim_id: int, rb, rw) -> tuple:
    """Dispatch the player FSM to the recovered per-state handler (the ``cs:[anim_id*2 + 0x7D2F]`` table).

    Every recovered handler behind one uniform ``(rb, rw) -> (writes, sfx)`` entry point, keyed by the
    ``anim_id`` from :func:`player_select_anim_id`. anim_ids 3/6/7 share the audio-coupled "attack" (door-bash/secret-reveal) handler
    (``0x5F96``), which also emits ``play_sfx`` commands (``sfx``); the others emit no sound (``[]``)."""
    if anim_id in (3, 6, 7):
        return player_state_attack(anim_id, anim_id * 2, rb, rw)
    handler = PLAYER_HANDLERS.get(anim_id)
    if handler is None:
        raise NotImplementedError(f"player FSM handler for anim_id={anim_id} not recovered")
    return handler(rb, rw), []


def player_tick_timers(timers: dict) -> dict:
    """Recover the player-update timer tail ``1030:5A47..5A87``.

    Decrement each of the seven byte countdown timers + the one word countdown timer, every one clamped at 0.
    Pure: ``timers`` maps each address in ``TIMER_BYTES``/``TIMER_WORD`` to its current value; returns the new
    values (same keys). Bytes are 8-bit-wrapped, the word is 16-bit."""
    out = {a: _dec_floor(timers[a], 8) for a in TIMER_BYTES}     # [5A4A-5A7E] seven byte timers
    out[TIMER_WORD] = _dec_floor(timers[TIMER_WORD], 16)         # [5A82-5A87] one word timer
    return out
