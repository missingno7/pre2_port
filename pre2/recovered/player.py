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

READABILITY (the offsets-into-views lift, same pattern as player_collision): the rb/rw-consuming handlers
read/write NAMED state — ``p`` = :class:`PlayerView`, ``g`` = :class:`PlayerGlobals` — with every field's
offset + width + [asm] evidence living once in pre2/views/dgroup_view. The leaf primitives (integrate, accel,
friction, gravity, set/advance anim) already take VALUES, not memory. Contracts are unchanged: the handlers
still return their plain ``{offset: value}`` dicts (widths implied by FSM_WORD_FIELDS); ``player_flying_484e``
keeps its ``{offset: (value, width)}`` convention via :class:`WidthContractBackend`. Raw offsets remain only
for genuinely dynamic data — the anim/impulse/phase TABLES, the trail ring, the projectile spawn slots.
"""
from __future__ import annotations

from pre2.views.dgroup_view import (DictBackend, PLAYER_BASE, PlayerGlobals, PlayerView, RENDER_SLOTS_BASE,
                                    WidthContractBackend)

__all__ = [
    "player_x_integrate", "player_y_integrate", "player_tick_timers",
    "player_accel", "player_friction_dir", "player_friction_sym", "player_gravity",
    "player_set_anim", "player_advance_anim", "player_select_anim_id",
    "player_state_run", "player_state_anim5", "player_state_idle", "player_state_jump", "player_state_anim8",
    "player_state_anim4", "player_state_attack", "player_dispatch_handler", "PLAYER_HANDLERS",
    "player_fsm_frontend", "player_fsm_step", "FSM_WORD_FIELDS", "SCROLL_REQUEST",
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

# player render/physics record fields (PLAYER_BASE = render slot 1; slot 0 sits just below it) + the
# player-state globals / per-frame timers the FSM writes -- named once here (= the like-named
# PlayerView/PlayerGlobals fields in dgroup_view; offsets given relative to the named slot base).
P_SPRITE   = PLAYER_BASE + 0x04   # 0x4F20 packed anim-frame word (same field as ANIM_FRAME below)
P_XVEL     = PLAYER_BASE + 0x06   # 0x4F22 X velocity
P_FACING   = PLAYER_BASE + 0x09   # 0x4F25 facing word
P_ANIM_B   = PLAYER_BASE + 0x0B   # 0x4F27 anim B-state
P_ANIM_PTR = PLAYER_BASE + 0x0C   # 0x4F28 anim-script pointer
P_YVEL     = PLAYER_BASE + 0x0E   # 0x4F2A Y velocity
P_RUN      = PLAYER_BASE + 0x10   # 0x4F2C run-state flag
SLOT0_DEATH = RENDER_SLOTS_BASE + 0x11   # 0x4F1B slot-0 life/death byte (the FSM's "depth")
POPUP_RING_HEAD = 0x6BBE          # popup/effect ring write cursor
RUN_COUNT = 0x6BEB                # run-frames counter
FIDGET_RANGE_TABLE = 0x79E0       # idle-fidget anim threshold ranges [lo,hi)
IDLE_TIMER = 0x6BD3               # idle/fidget clock gate
ANIM_GATE = 0x6BD0                # hold-current-anim / FSM-route gate
# the 5A4A-5A87 per-frame countdown timers (seven byte counters + the 0x6BE2 word)
CHARGE = 0x6BCE
INPUT_SUPPRESS = 0x6BCD
SHAKE_MAGNITUDE = 0x6BEA
TIMER_6BE8 = 0x6BE8
RESPAWN_STATE = 0x6BE4
PENDING_PICKUP = 0x6BE1
REWARD_ARM_HI = 0x6C00

# 16-bit-word DS fields written by player_fsm_step; every other write in its contract is a byte. Used by the
# live FSM checkpoint to apply / diff each write at the right width (the byte-vs-word lesson from collision).
FSM_WORD_FIELDS = frozenset(
    {RENDER_SLOTS_BASE, RENDER_SLOTS_BASE + 2, RENDER_SLOTS_BASE + 4, P_SPRITE, P_XVEL, P_FACING,
     P_ANIM_PTR, P_YVEL, POPUP_RING_HEAD, RUN_COUNT}
    | {s + d for s in range(ATTACK_SPAWN_LIST, ATTACK_SPAWN_LIST + 4 * 0x12, 0x12)  # projectile slots:
       for d in (0, 2, 4, 6, 0xC, 0xE)}                                            # words (the +8 field is a byte)
    | set(range(TRAIL_RING_LO, TRAIL_RING_HI + 6, 2))    # trail/dust ring slots, incl. the TOP slot's +4 id word (0x4FC2):
    #   5E11/5E41 write [ring+4]=0x35 as a WORD (mov word ptr) — a byte write leaves [+5] stale (was 0x4FC2-exclusive)
)

SCROLL_REQUEST = "__scroll"  # sentinel key in the writes dict: the idle look-around camera-pan request the
                             # caller executes against the VM planes (player_fsm_step pops it into its return)

ANIM_SEQ_TABLE = 0x7CDF   # [asm 6366/637D] base of the per-state animation-sequence pointer table
ANIM_ID_TABLE = 0x7B7F    # [asm 592E] base of the input-bitmask -> anim_id (FSM state index) table

XVEL_FLOOR = -0x60      # [asm 62FA] directional-friction floor on Xvel

# [asm 5A4A-5A87] per-frame countdown timers decremented at the tail of the player update, each clamped at 0
# (`sub [x],1 ; adc [x],0` = decrement-but-not-below-zero). Seven byte counters + one word counter.
TIMER_BYTES = (CHARGE, INPUT_SUPPRESS, SHAKE_MAGNITUDE, TIMER_6BE8, RESPAWN_STATE, PENDING_PICKUP,
               REWARD_ARM_HI)
TIMER_WORD = 0x6BE2   # also object_update's sprite "scale" level + player_interaction's instadeath gate (same offset)

X_MIN = 0x0008          # [asm 5A29] commit only if new_x >= 8 (left world edge)
X_MAX = 0x0FF8          # [asm 5A2E] commit only if new_x < 0xFF8 (right world edge)
VIEW_TILES = 0x14       # [asm 5A20] the viewport width in tiles added to the camera-left tile


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _views(rb, rw, out: dict | None = None):
    """Bind the named views over the caller's DS readers; named writes record into ``out`` (or a fresh dict)
    in the FSM's plain ``{offset: value}`` contract (widths implied by FSM_WORD_FIELDS at apply time)."""
    be = DictBackend(rb, rw, out)
    return PlayerView(be), PlayerGlobals(be), be


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


def player_gravity(yvel: int, low_gravity: int, limit: int) -> int:
    """Recover the player gravity ``1030:6309``.

    Add gravity to Yvel — ``0x10`` normally, ``4`` when the low-gravity flag ``[0x6BC7]==1`` (with the terminal
    velocity ``limit`` also divided by 8) — then cap at the terminal velocity. ``[0x6BC7]`` is the GLIDE /
    float-descent gate (armed at the jump apex [5F..], gates the flight animation [63xx], cleared on landing
    [642D]) — NOT water; Prehistorik 2 has no water. Pure: returns the new ``[0x4F2A]``."""
    grav = 0x10                                                         # [6310]
    term = _s16(limit & 0xFFFF)
    if low_gravity == 1:                                               # [6313-6321]
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
    writes = {SLOT0_DEATH: depth & 0xFF}                              # [5932-5936] [0x4F1B]=[0x4F2D]
    if (depth & 0xFF) >= 0x16:                                   # [593A-593F]
        anim_id = 8
    if (anim_b_state & 0xFF) != anim_id:                         # [5941] anim changed -> reset run state
        beb = 0                                                  # [5947]
        writes[P_RUN] = 0                                       # [594D]
    writes[RUN_COUNT] = _inc_wrap_word(beb)                         # [5952-5957]
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


ANIM_FRAME = 0x4F20          # player animation frame word
GLIDER_GATE = 0x6BC5         # [asm 484E] nonzero => glider/flying mode active
GLIDER_TILT = 0x7B1A         # tilt/pitch state (0..6, neutral = 3)
GLIDER_TILT_TABLE = 0x7B1B   # 7 words: the tilt-table wing anims (indexed by tilt)
GLIDER_ANIM_TABLE = 0x7B29   # {frame_key: word, wing_anim: word, xoff: s8, yoff: s8} stride 6, cx==0 terminated


def player_flying_484e(rb, rw):
    """[asm 1030:484E] The glider / flying-mode per-frame update — active only while the flying gate ``[0x6BC5]``
    is set (the glider pickup arms it). Two parts:

    * compute the flight-animation frame ``[0x4F20]`` (only when ``[0x6BC7]!=0``) from ``|Xvel|`` (``[0x4F22]``:
      >0x40 / 0x21..0x40 / <=0x20 -> base 0x30/0x31/0x32) OR, when the tilt ``[0x7B1A]`` is below neutral (<3), a
      banked frame ``0x33-tilt``; the facing bit (0x8000) is preserved;
    * auto-return the tilt toward neutral (3) by ±1 when there is no up/down input (``[0x27EA]|[0x27EB]==0``),
      then scan the ``0x7B29`` anim table (stride 6, ``cx==0`` = end) for the entry whose key matches the frame
      ``[0x4F20]&0x1FFF`` and place the glider WING sprite as render slot 0: ``[0x4F0A]``=playerX+xoff,
      ``[0x4F0C]``=playerY+yoff, ``[0x4F0E]``=the wing anim (a >=0x79 wing anim indexes the tilt table; facing
      left ORs 0x8000 into it and negates the X offset; ``[0x6BC8]>0x18`` with a low byte <0x79 bumps it by 1).

    Returns the ``{offset: (value, width)}`` write contract. Empty (a no-op) when ``[0x6BC5]==0``. The gameplay
    writes are ``[0x4F20]`` (anim) + ``[0x7B1A]`` (tilt); the wing slot ``[0x4F0A/0C/0E]`` is a render record."""
    be = WidthContractBackend(rb, rw)
    p, g = PlayerView(be), PlayerGlobals(be)
    if g.glider == 0:                                             # [484E] not flying
        return {}
    out = be.writes
    frame = p.sprite
    if g.low_gravity != 0:                                        # [485D] descending -> recompute the flight frame
        f = frame & 0x8000                                         # [485F] keep the facing bit
        ax = abs(p.xvel)                                           # [4868-486F] |Xvel|
        dx = 0x30                                                  # [4865]
        if ax <= 0x40:                                            # [4871-487C]
            dx += 1
            if ax <= 0x20:
                dx += 1
        tilt = g.glider_tilt                                       # [487D]
        if tilt < 3:                                              # [4880-4886] banked below neutral
            dx = (0x33 - tilt) & 0xFF
        frame = (f | dx) & 0xFFFF                                 # [4888]
        p.sprite = frame
    dl = (frame >> 8) & 0xFF                                      # [4892] facing/high byte
    key = frame & 0x1FFF                                          # [4894] and ah,0x1f
    tilt = g.glider_tilt
    if (g.in_up | g.in_down) == 0:                                # [4897-489F] no up/down -> auto-return the tilt
        if tilt != 3:                                            # [48A3-48AC] step toward neutral
            tilt = (tilt + (1 if tilt < 3 else -1)) & 0xFF        # [48AE] writes the tilt IN MEMORY, so the
            g.glider_tilt = tilt                                  #   tilt-table lookup below sees this new value
    si = GLIDER_ANIM_TABLE                                        # [488C]
    while True:
        cx = rw(si)                                              # [48B2]
        if cx == 0:                                             # [48B4] end of table
            return out
        if key == cx:                                           # [48B6] matched frame
            xoff = _s8(rb(si + 4))                               # [48BA-48BE]
            wing = rw(si + 2)                                   # [48C0]
            if wing >= 0x79:                                    # [48C3] -> the tilt table (indexed by the
                wing = rw((tilt * 2 + GLIDER_TILT_TABLE) & 0xFFFF)   # [48C8-48D0] auto-returned tilt, not entry)
            if dl & 0x80:                                        # [48D4-48DB] facing left
                wing = (wing | 0x8000) & 0xFFFF
                xoff = -xoff
            if g.fly_timer > 0x18 and (wing & 0xFF) < 0x79:      # [48DD-48E8]
                wing = (wing + 1) & 0xFFFF
            p.slot0.sprite = wing                                # [48E9] the wing anim (render slot 0)
            p.slot0.x = (p.x + xoff) & 0xFFFF                    # [48EC-48F1] wing X
            p.slot0.y = (p.y + _s8(rb(si + 5))) & 0xFFFF         # [48F4-48FC] wing Y
            return out
        si = (si + 6) & 0xFFFF                                   # [4901]


def player_state_run(rb, rw) -> dict:
    """Recover the ``anim_id==1`` "run" FSM handler ``1030:5EC4`` (the normal-play main path).

    The handler is a composition of the recovered primitives (the original source structure). With entry
    ``al==1`` (anim_id) and ``bx==2`` (anim_id*2 = the sequence index) preserved through the calls, the main
    path (gates ``[0x6BD0]==0`` no override, ``[0x6BC5]==0`` no flying-state block) is::

        [0x6BD3] = sat_inc([0x6BD3])              # 5EF9 frame counter (caps at 0xFF)
        [0x4F22] = accel(limit=0x50)              # 5F03-5F06 player_accel
        [0x4F22] = friction_dir([0x4F22])         # 5F09 player_friction_dir
        ptr      = set_anim_b(anim=1, seq=2)      # 5F0C player_set_anim ([0x4F27]/[0x4F28])
        advance_anim(ptr)                         # 5F0F player_advance_anim ([0x4F20]/[0x4F28]/[0x6BCF])

    ``rb``/``rw`` read entry memory; returns the dict of writes. Pure."""
    p, g, be = _views(rb, rw)
    out = be.writes
    # [asm 5ECE-5EF8] the FLYING-state block: while gliding at speed (|Xvel|>=0x40), count the flying
    # timer up (saturating), dropping ONE trail sprite (5E18 = the ungated 5E11 emit) when it hits 0x17.
    if g.glider != 0 and abs(p.xvel) >= 0x40:                                            # [5ED8-5EE2]
        bc8 = _sat_inc_byte(g.fly_timer)                                                 # [5EE4-5EE9] sat_inc
        g.fly_timer = bc8
        if bc8 == 0x17:                                                                  # [5EEE-5EF5] -> 5E18 trail
            emit = player_emit_trail(p.x, p.y, 0, g.trail_ring)                          # blink 0 = ungated emit
            if emit is not None:
                out.update(emit[0])
                g.trail_ring = emit[1]
    g.idle_timer = _sat_inc_byte(g.idle_timer)                                           # [5EF9-5EFE]
    xvel = player_accel(p.xvel & 0xFFFF, p.facing, p.motion_mode, g.input_lr != 0, RUN_ACCEL_LIMIT)  # [5F03-5F06]
    xvel = player_friction_dir(xvel, g.friction)                                         # [5F09]
    p.xvel = xvel
    # [5F0C] set_anim_b: the seq index is the dispatch `bx` (anim_id*2), which the flying gate (5960) bumped by
    # 0x40 — so gliding plays the FLYING run sequence (0x42) instead of the ground-run one (0x02).
    seq = 0x42 if g.glider != 0 else 2
    state, ptr = player_set_anim(1, seq, p.anim_b, p.anim_ptr, rw)
    p.anim_b = state
    frame, new_ptr, bcf = player_advance_anim(ptr, p.facing_lo, rw)                      # [5F0F]
    p.anim_ptr = new_ptr
    p.sprite = frame
    g.anim_hi = bcf
    return out


def player_emit_trail(player_x: int, player_y: int, blink: int, ring_ptr: int):
    """Recover the player trail-sprite emitter ``1030:5E11`` (called from the moving-idle path).

    Gated to every 4th frame (``[0x6BD5] & 3 == 0``): push a sprite ``(x, y, id=0x35)`` into the ring buffer at
    ``[0x6BBE]`` then step the pointer back by 0x12, wrapping below ``0x4F76`` to ``0x4FBE``. Returns ``None``
    when gated, else ``(word_writes, new_ring_ptr)`` where ``word_writes`` maps ring offsets to 16-bit values."""
    if blink & 3:                                               # [5E11-5E16] gated
        return None
    from pre2.views.dgroup_view import RenderSlot
    be = DictBackend(lambda o: 0, lambda o: 0)                  # write-only: the slot fields fill the contract
    slot = RenderSlot(be, ring_ptr & 0xFFFF)                    # the ring slot the cursor points at
    slot.x = player_x                                           # [5E1E-5E21]
    slot.y = player_y                                           # [5E23-5E26]
    slot.sprite = TRAIL_SPRITE                                  # [5E29] 0x35
    bx = (ring_ptr - TRAIL_STRIDE) & 0xFFFF                     # [5E2E] step the cursor down...
    if bx < TRAIL_RING_LO:                                      # [5E31-5E37] ...wrapping to the top slot
        bx = TRAIL_RING_HI
    return be.writes, bx


def player_charge_6bce(v: int) -> int:
    """Recover the small shared helper ``1030:5EB7`` — grow the ``[0x6BCE]`` counter by 2 while it is <= 0x30
    (used by the anim_id 4 & 5 handlers; ``[0x6BCE]`` is also one of the per-frame timers)."""
    v &= 0xFF
    return (v + 2) & 0xFF if v <= 0x30 else v


def player_state_anim5(rb, rw, anim_id: int = 5) -> dict:
    """Recover the ``5E96`` FSM handler — the NORMAL ``anim_id==5`` state AND the FLYING dispatch's catch-all
    (``cs:[0x7D6F]`` sends anim_id 0/3/4/6/7 here). The dispatch enters with ``al=anim_id`` and ``bx=anim_id*2``
    (bumped by ``0x40`` under the flying gate), and both flow straight into ``set_anim_b`` — so the sequence is
    ``anim_id*2 (+0x40 gliding)``, not a hardcoded 5/0x0A::

        [0x6BC8]=0 ; [0x6BE1]=4                      # 5EA0/5EA5
        ptr = set_anim_b(anim=anim_id, seq=bx)       # 5EAA player_set_anim ([0x4F27]/[0x4F28])
        advance_anim(ptr)                            # 5EAD player_advance_anim ([0x4F20]/[0x4F28]/[0x6BCF])
        [0x4F22] = friction_sym([0x4F22])            # 5EB0 player_friction_sym
        [0x6BCE] = charge_6bce([0x6BCE])             # 5EB3 -> 5EB7

    ``rb``/``rw`` read entry memory; returns the dict of writes. Pure."""
    p, g, be = _views(rb, rw)
    g.fly_timer = 0                                                                       # [5EA0]
    g.drop_gate = 4                                                                       # [5EA5]
    seq = (anim_id * 2 + (0x40 if g.glider != 0 else 0)) & 0xFF                           # [dispatch bx]
    state, ptr = player_set_anim(anim_id, seq, p.anim_b, p.anim_ptr, rw)                  # [5EAA]
    p.anim_b = state
    frame, new_ptr, bcf = player_advance_anim(ptr, p.facing_lo, rw)                       # [5EAD]
    p.anim_ptr = new_ptr
    p.sprite = frame
    g.anim_hi = bcf
    p.xvel = player_friction_sym(p.xvel & 0xFFFF, p.motion_mode)                          # [5EB0]
    g.charge = player_charge_6bce(g.charge)                                               # [5EB3->5EB7]
    return be.writes


def _idle_set_advance(p: PlayerView, g: PlayerGlobals, anim_id: int, seq: int, rb, rw, facing: int) -> None:
    """Idle helper: ``set_anim_a`` (635D, tracks ``run_flag``) then ``advance_anim`` (638B)."""
    state, ptr = player_set_anim(anim_id, seq, p.run_flag, p.anim_ptr, rw)
    p.run_flag = state
    frame, new_ptr, bcf = player_advance_anim(ptr, facing & 0xFF, rw)
    p.anim_ptr = new_ptr
    p.sprite = frame
    g.anim_hi = bcf


def _idle_default_anim(p: PlayerView, entry_bx: int, facing: int, rw) -> None:
    """Idle "default" anim path ``1030:5DED`` — load the sequence for the handler's entry ``bx`` (anim_id*2;
    0 for a direct idle, but e.g. 4 when the jump handler falls through) and write frame 0 WITHOUT advancing
    and WITHOUT the 0x1F mask (only the facing bit is merged); resets ``run_flag``."""
    ptr = rw((entry_bx + ANIM_SEQ_TABLE) & 0xFFFF)              # [5DED] bx = [entry_bx + 0x7CDF]
    p.anim_ptr = ptr                                             # [5DF1]
    ax = rw(ptr)                                                 # [5DF5]
    ah = ((ax >> 8) | (facing & 0x80)) & 0xFF                    # [5DF7-5DFE] no 0x1F mask here
    p.sprite = ((ah << 8) | (ax & 0xFF)) & 0xFFFF               # [5E00]
    p.run_flag = 0                                               # [5E03]


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
    p, g, be = _views(rb, rw)
    out = be.writes
    g.fly_timer = 0                                             # [5CE8]
    xv = player_friction_dir(p.xvel & 0xFFFF, g.friction)       # [5CED]
    xv = player_friction_sym(xv, p.motion_mode)                 # [5CF0]
    p.xvel = xv

    if g.unk_6BFE == 0 and (p.yvel & 0xFFFF) != 0:              # [5CF3-5CFF] airborne (in the air)
        if g.fall_latch > 4:                                    # [5D01] jbe
            p.xvel = player_friction_dir(xv, g.friction)        # [5D08]
        return out                                              # [5D0B] jmp 5E0D (no anim_b reset)

    facing = p.facing_lo
    ax = abs(_s16(out[P_XVEL]))                                 # [5D0E-5D15] |Xvel| (post-friction)
    if ax >= 8:                                                 # [5D17] jb 5D42
        if ((p.flags >> 7) & 1) == ((out[P_XVEL] >> 15) & 1):   # [5D1C-5D2C] facing == vel sign?
            _idle_set_advance(p, g, 0x12, 0x24, rb, rw, facing)     # [5D31-5D39] anim 0x12
            trail = player_emit_trail(p.x, p.y, g.frame_blink, g.trail_ring)  # [5D3C] call 5E11
            if trail is not None:
                out.update(trail[0])
                g.trail_ring = trail[1]
        else:
            _idle_default_anim(p, entry_bx, facing, rw)                # [5D2E] jmp 5DED
        p.anim_b = 0                                            # [5E08]
        return out

    if _s16(out[P_XVEL]) != 0:                                  # [5D42-5D46] 0 < |Xvel| < 8 -> default
        _idle_default_anim(p, entry_bx, facing, rw)
        p.anim_b = 0
        return out

    # Xvel == 0 [5D49]
    timer = g.idle_timer
    e9 = g.in_aux
    eced = g.in_right & g.in_left
    if timer >= 0x1E:                                           # [5D49] jb 5D73
        if e9 == 0 and eced == 0:                               # [5D50-5D5E] no input -> long idle
            g.idle_timer = timer - 3                            # [5D60]
            _idle_set_advance(p, g, 0x10, 0x20, rb, rw, facing)  # [5D65-5D6D] anim 0x10
            p.anim_b = 0
            return out
        reach_5d83 = True                                      # input present -> 5D83
    else:                                                       # [5D73]
        if eced != 0:
            reach_5d83 = True
        elif e9 == 0:
            reach_5d83 = False                                 # [5D7C] je 5DC9 (fidget)
        else:
            reach_5d83 = True

    if reach_5d83 and g.unk_6BFE == 0:                          # [5D83] jne 5DC9 ; else 5D8A — look-around
        _idle_set_advance(p, g, 0x13, 0x26, rb, rw, facing)    # [5D8A-5D92] set_anim_a 0x13 + advance
        if not (g.level_flags & 2) and g.unk_6BD9 == 0:        # [5D95-5DA1] camera-pan gates
            screen_x = (_s16(p.x) >> 4) - _s16(g.cam_col_word)  # [5DA3-5DAA] player tile X - camera X
            if p.flags & 0x80:                                 # [5DAE] facing left -> 3414
                if (screen_x & 0xFFFF) < 0x11:                 # [5DBF-5DC2] jae 5E08
                    out[SCROLL_REQUEST] = "left"               # [5DC4 call 3414]
            elif (screen_x & 0xFFFF) > 2:                      # [5DB5-5DB8] facing right, jbe 5E08 -> 3435
                out[SCROLL_REQUEST] = "right"                  # [5DBA call 3435]
        p.anim_b = 0                                            # [5E08]
        return out

    # fidget [5DC9]: find the 0x79E0 range [lo,hi) containing key=idle_clock&0x1FF -> anim 0x11; below lo -> default
    key = g.idle_clock & 0x1FF
    si = FIDGET_RANGE_TABLE
    while True:
        if key < rw(si):                                       # [5DD2] jb 5DED
            _idle_default_anim(p, entry_bx, facing, rw)
            p.anim_b = 0
            return out
        if key < rw((si + 2) & 0xFFFF):                        # [5DD6] jb 5DE0
            _idle_set_advance(p, g, 0x11, 0x22, rb, rw, facing)  # [5DE0-5DE8] anim 0x11
            p.anim_b = 0
            return out
        si = (si + 4) & 0xFFFF                                 # [5DDB]


def player_state_jump(rb, rw) -> dict:
    """Recover the ``anim_id==2`` "jump/rising" FSM handler ``1030:5F30`` (main path, gate ``[0x6BD0]==0``).

    Falls through to the idle handler when ``[0x6BE0]!=0`` (entering with ``bx==4``). Otherwise: drive the jump
    arc — for the first ``9`` frames add the decaying impulse ``[0x79CE + counter*2]`` to Yvel (counter is
    ``[0x6BD1]``, post-incremented), then switch to gravity; apply horizontal control (accel toward 0x30 when
    Xvel is small, else symmetric friction); ``set_anim_b(2, seq=4)`` + advance; finally two directional
    frictions. ``rb``/``rw`` read entry memory; returns the dict of writes."""
    _p, g, _be = _views(rb, rw)
    if g.fall_grace != 0:                                        # [5F37-5F3E] jmp 5CDB
        return player_state_idle(rb, rw, entry_bx=4)
    return _jump_body(rb, rw)


def _jump_body(rb, rw) -> dict:
    """The jump-arc body ``1030:5F41`` — shared by :func:`player_state_jump` (after its ``[0x6BE0]`` prologue) and
    the flying-mode jump (``5F13``). Drives the rising arc (decaying impulse table for the first 9 frames, then
    gravity), horizontal control, ``set_anim_b(2)``, and two directional frictions. Under the flying gate
    (``[0x6BC5]!=0``) the impulse is halved (``5F5B-5F62``); when ``[0x6BC5]==0`` that is a no-op, so the normal
    jump is unchanged."""
    p, g, be = _views(rb, rw)
    g.unk_6BFE = 0                                               # [5F41]
    counter = g.fall_latch                                       # [5F46] (the jump arc's frame counter)
    g.fall_latch = counter + 1                                   # [5F4C] inc
    if counter < JUMP_FRAMES:                                    # [5F50] jae
        impulse = rw((JUMP_IMPULSE_TABLE + counter * 2) & 0xFFFF)   # [5F55-5F57]
        if g.glider != 0:                                       # [5F5B-5F62] flying state -> halve the impulse
            impulse = (_s16(impulse) >> 1) & 0xFFFF
        p.yvel = ((p.yvel & 0xFFFF) + impulse) & 0xFFFF         # [5F64] Yvel += impulse
    else:
        p.yvel = player_gravity(p.yvel & 0xFFFF, g.low_gravity, 0xC0)   # [5F6A-5F6D] gravity

    xvel = p.xvel & 0xFFFF
    if xvel < 0x30:                                             # [5F73] jb (unsigned)
        xvel = player_accel(xvel, p.facing, p.motion_mode, g.input_lr != 0, 0x30)   # [5F7E]
    else:
        xvel = player_friction_sym(xvel, p.motion_mode)         # [5F79]

    state, ptr = player_set_anim(2, 4, p.anim_b, p.anim_ptr, rw)     # [5F81-5F86] set_anim_b
    p.anim_b = state
    frame, new_ptr, bcf = player_advance_anim(ptr, p.facing_lo, rw)  # [5F89]
    p.anim_ptr = new_ptr
    p.sprite = frame
    g.anim_hi = bcf

    xvel = player_friction_dir(xvel, g.friction)                # [5F8C]
    xvel = player_friction_dir(xvel, g.friction)                # [5F8F]
    p.xvel = xvel
    return be.writes


def _flying_jump(rb, rw) -> dict:
    """The flying-mode jump ``1030:5F13`` (``cs:[0x7D6F][2]``). Once the hold counter ``[0x6BC8]`` reaches
    0x18 the jump ends (arm the descent: ``[0x6BC7]=1``, ``[0x6BC6]=0x18``, nudge ``Y-=3``, reset ``[0x6BC8]``)
    and only the two directional frictions run; otherwise it runs the shared jump body."""
    p, g, be = _views(rb, rw)
    if g.fly_timer >= 0x18:                                     # [5F13-5F18]
        g.low_gravity = 1                                       # [5F1A-5F29] arm the descent
        g.fly_hold = 0x18
        g.fly_timer = 0
        p.y = (p.y - 3) & 0xFFFF
        xvel = player_friction_dir(p.xvel & 0xFFFF, g.friction)  # [5F2E jmp 5F8C]
        xvel = player_friction_dir(xvel, g.friction)            # [5F8F]
        p.xvel = xvel
        return be.writes
    return _jump_body(rb, rw)                                   # [5F41]


def player_fsm_flying(rb, rw) -> tuple:
    """Recover the flying-movement state machine ``1030:596A`` (the ``[0x6BC5]!=0`` branch taken at the
    FSM gate ``5960``). Returns ``(writes, do_dispatch)``: when ``do_dispatch`` is True the caller then runs the
    **flying** handler table ``cs:[0x7D6F]`` (the normal dispatch with ``bx += 0x40``).

    Only the ``[0x6BC7]&1 == 0`` branch is witnessed (the flying hold has not started): mask ``[0x6BC7]``,
    arm the descent flag when Yvel exceeds 0xA0, then dispatch. The input-driven hold bookkeeping (``5977``,
    over ``[0x6BC6]``/``[0x7B1A]`` from ``[0x27EA]``/``[0x27EB]``) is unwitnessed and fails loud."""
    p, g, be = _views(rb, rw)
    out = be.writes
    bc7 = g.low_gravity & 1                                    # [596D] and (descend flag),1
    g.low_gravity = bc7
    if bc7 == 0:                                              # [5972] not held -> 59FE then dispatch
        if p.yvel > 0xA0:                                      # [59FE-5A04] jg
            g.low_gravity = 1                                  # [5A06]
        return out, True                                      # [-> 5A0B] dispatch via cs:[0x7D6F]

    # [5977] the flying hold is active (descend bit0): bookkeeping over fly_hold/tilt; never dispatches.
    ea, eb = g.in_up, g.in_down
    bc6, b1a = g.fly_hold, g.glider_tilt
    if ea != 0:                                               # [5977] held "up"
        if b1a == 6:                                          # [597E]
            if p.yvel > 0x10:                                 # [5985] jle 5997
                bc7 |= 2                                      # [598C]
        else:
            b1a = (b1a + 1) & 0xFF                            # [5993] inc the tilt
        if bc6 != 0:                                          # [5997] je 59B1
            bc6 = (bc6 - 1) & 0xFF                            # [599E]
            g.fly_hold = bc6
            g.low_gravity = bc7
            g.glider_tilt = b1a
            if b1a >= 4:                                      # [59A2] jb 5A0F
                p.yvel = 0xFFC0                               # [59A9]
            return out, False
        g.low_gravity = bc7                                   # bc6 == 0 -> fall to 59B1
        g.glider_tilt = b1a
    # [59B1] (ea==0, or held-up with fly_hold==0)
    if eb == 0:                                               # [59B1] not held "down"
        g.fly_hold = _dec_floor(bc6, 8)                       # [59B8] saturating dec
        return out, False
    if b1a != 0:                                              # [59C4] held "down"
        b1a = (b1a - 1) & 0xFF                                # [59CB] dec the tilt
    g.low_gravity = bc7 | 2                                   # [59CF] or (descend),2 (out value == bc7 on
    g.glider_tilt = b1a                                       #   every path reaching here)
    if b1a > 1:                                               # [59D4] ja 5A0F
        return out, False
    mag = abs(p.xvel >> 4) & 0xFF                             # [59DC-59E9] |sar(Xvel,4)|
    if bc6 < ((mag * 5) & 0xFF):                              # [59ED-59F5] cmp fly_hold,mag*5 ; jae
        g.fly_hold = (bc6 + mag) & 0xFF                       # [59F7] add fly_hold,mag
    return out, False


def player_fsm_flying_dispatch(anim_id: int, rb, rw) -> tuple:
    """The flying handler table ``cs:[0x7D6F]`` (``= cs:[0x7D2F] + 0x40``). Mostly reuses recovered handlers:
    anim_id 1 -> run (5EC4), 2 -> the flying jump (5F13), 8 -> no-op (454C = ret); everything else -> anim5
    (5E96). Returns ``(writes, sfx)``."""
    if anim_id == 1:
        return player_state_run(rb, rw), []                    # [5EC4]
    if anim_id == 2:
        return _flying_jump(rb, rw), []                      # [5F13]
    if anim_id == 8:
        return {}, []                                          # [454C] ret
    return player_state_anim5(rb, rw, anim_id), []             # [5E96] idx 0/3/4/5/6/7 (dispatched anim_id + bx+0x40)


def player_state_anim8(rb, rw) -> dict:
    """Recover the ``anim_id==8`` FSM handler ``1030:5CCE`` (the depth-override state; no ``[0x6BD0]`` gate).

    ``friction_dir; friction_sym; set_anim_b; advance_anim``. NOTE the register-flow gotcha: ``friction_sym``
    (6333) leaves ``ax`` = the new Xvel, so the following ``set_anim_b`` (6374) is called with ``al`` = that
    Xvel's low byte (NOT the anim_id) and ``bx`` == 0x10 (anim_id*2) as the sequence index. Faithful to the
    ASM. ``rb``/``rw`` read entry memory; returns the dict of writes."""
    p, g, be = _views(rb, rw)
    xv = player_friction_dir(p.xvel & 0xFFFF, g.friction)      # [5CCE]
    xv = player_friction_sym(xv, p.motion_mode)                # [5CD1] -> ax = xv
    p.xvel = xv
    state, ptr = player_set_anim(xv & 0xFF, 0x10, p.anim_b, p.anim_ptr, rw)     # [5CD4] al = xv low byte
    p.anim_b = state
    frame, new_ptr, bcf = player_advance_anim(ptr, p.facing_lo, rw)             # [5CD7]
    p.anim_ptr = new_ptr
    p.sprite = frame
    g.anim_hi = bcf
    return be.writes


def player_state_anim4(rb, rw) -> dict:
    """Recover the ``anim_id==4`` FSM handler ``1030:5E62`` (main path, gate ``[0x6BD0]==0``).

    Always: ``[0x6BD3]=0``, ``[0x6BE1]=4``, ``charge_6bce``. Then on ``|Xvel| <= 0x20`` accelerate (limit 0x20)
    + ``set_anim_b`` + advance (``al`` = the clobbered ``|Xvel|`` low byte, ``bx``==8); otherwise fall through
    to the idle handler (bx==8), which — because ``[0x6BD3]`` was just zeroed — sees a fresh idle timer."""
    p, g, be = _views(rb, rw)
    out = be.writes
    g.idle_timer = 0                                                       # [5E6C]
    g.drop_gate = 4                                                        # [5E71]
    g.charge = player_charge_6bce(g.charge)                                # [5E76]
    mag = abs(p.xvel)                                                      # [5E79]
    if mag <= 0x20:                                                        # [5E85-5E87] jbe -> accel
        p.xvel = player_accel(p.xvel & 0xFFFF, p.facing, p.motion_mode, g.input_lr != 0, 0x20)  # [5E8C]
        state, ptr = player_set_anim(mag & 0xFF, 8, p.anim_b, p.anim_ptr, rw)     # [5E8F] al = clobbered |Xvel|
        p.anim_b = state
        frame, new_ptr, bcf = player_advance_anim(ptr, p.facing_lo, rw)           # [5E92]
        p.anim_ptr = new_ptr
        p.sprite = frame
        g.anim_hi = bcf
        return out
    # [5E89] jmp 5CDB — idle sees idle_timer==0 (just written) and bx==8
    rb2 = lambda o: 0 if o == IDLE_TIMER else rb(o)                            # noqa: E731 — the just-written read shim
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
    p, g, be = _views(rb, rw)
    ec, ed = g.in_right, g.in_left
    ea, eb, e8 = g.in_up, g.in_down, g.in_fire
    g.input_lr = ed | ec                                         # [58A7-58B0]
    g.input_ud = ea | eb                                         # [58B4-58BB]
    facing = p.facing & 0xFFFF                                   # [58BF-58FC] facing update (raw word compare)
    if ec != 0:
        if ed == 0 and facing != 1:
            p.facing = 1
            g.run_count = 0
    elif ed != 0:
        if facing != 0xFFFF:
            p.facing = 0xFFFF
            g.run_count = 0
    bitmask = 0                                                  # [58FC-591F] pack bit0 of the 5 flags
    for flag in (ec, ed, ea, eb, e8):
        bitmask = ((bitmask << 1) | (flag & 1)) & 0xFF
    return bitmask, be.writes


def player_fsm_step(rb, rw) -> tuple:
    """Compose the full per-frame player FSM ``1030:58A7..5A0B`` (the ``[0x6BC5]==0`` normal-play path):
    front-end -> ``select_anim_id`` -> dispatch to the recovered handler. Returns ``(writes, sfx, scroll)`` where
    ``scroll`` is ``None`` or ``"left"``/``"right"`` — the idle look-around (anim13) camera-pan request, which the
    caller executes against the VM planes via :func:`pre2.views.camera_pan.apply_camera_pan`.

    Threads the intermediate writes the way the ASM does: the facing/state changes from the front-end and the
    ``[0x4F2C]`` reset from the selector are visible to the handler (it reads ``[0x4F25]``/``[0x4F2C]``)."""
    p0, g0, _be0 = _views(rb, rw)                               # read-only views over ENTRY memory
    bitmask, writes = player_fsm_frontend(rb, rw)               # [58A7-591F]
    beb = writes.get(RUN_COUNT, g0.run_count)
    anim_id, sel_writes = player_select_anim_id(bitmask, g0.input_suppress, p0.death_state,  # [5921-595C]
                                                p0.anim_b, beb, rb)
    writes.update(sel_writes)                                   # [0x4F1B], [0x6BEB], maybe [0x4F2C]

    # The handler reads back fields the front-end/selector just wrote — facing [0x4F25], the [0x4F2C] reset,
    # and crucially the input-held flags [0x6BDB]/[0x6BDC] that drive player_accel. Expose every pending write
    # through a read overlay (the ASM reads them from memory mid-routine).
    def rb2(off):
        return (writes[off] & 0xFF) if off in writes else rb(off)

    def rw2(off):
        return (writes[off] & 0xFFFF) if off in writes else rw(off)

    # [5960] flying gate: when the glider is armed the FSM runs the 596A state machine instead of the
    # normal dispatch, then (if it falls through) dispatches the flying handler table cs:[0x7D6F].
    if g0.glider != 0:
        mwrites, do_dispatch = player_fsm_flying(rb2, rw2)   # [596A]
        writes.update(mwrites)
        msfx: list = []
        if do_dispatch:                                        # [-> 5A0B] dispatch via cs:[0x7D6F]
            if rb2(ANIM_GATE) != 0:                               # the flying handlers' [0x6BD0] override is unwitnessed
                raise NotImplementedError("flying dispatch with [0x6BD0]!=0 not witnessed")
            hw, msfx = player_fsm_flying_dispatch(anim_id, rb2, rw2)
            writes.update(hw)
        return writes, msfx, writes.pop(SCROLL_REQUEST, None)

    # Handlers for anim_id 0/1/2/4/5 begin with `cmp [0x6BD0],0 ; jne 5F93` — when the override flag is set they
    # run the shared override tail 0x5F93 (== the attack body with al=[0x4F27]). The attack handler itself
    # (anim_id 3/6/7 = 5F96, the tail's own body) has no such prologue — it can't override into itself — and
    # anim_id 8 (5CCE) has none either; both dispatch normally.
    if g0.anim_gate != 0 and anim_id not in (3, 6, 7, 8):       # [5F35-etc] -> 5F93 override
        hw, sfx = player_state_attack(rb2(P_ANIM_B), anim_id * 2, rb2, rw2)
    else:
        hw, sfx = player_dispatch_handler(anim_id, rb2, rw2)    # [5A0B] call cs:[anim_id*2 + 0x7D2F]
    writes.update(hw)
    scroll = writes.pop(SCROLL_REQUEST, None)                   # idle look-around (anim13) camera-pan request
    return writes, sfx, scroll


def _attack_render_sprite(out: dict, rec: int, frame: int, rb, rw) -> None:
    """1030:6081 — map the current anim frame to the player render sprite via the phase's frame table (8-byte
    records {frame, sprite_id, x_off, y_off}, 0x55AA terminator). Sets [0x4F0E]/[0x4F0A]/[0x4F0C] with the
    facing flip; leaves them unchanged if the frame is not in the table."""
    p, _g, _be = _views(rb, rw, out)                            # reads see the caller's pending writes (rw = overlay)
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
    p.slot0.sprite = sprid & 0xFFFF                             # [60AE] the player render sprite (slot 0)
    p.slot0.x = (p.x + (p.xvel >> 4) - _s16(cx)) & 0xFFFF       # [60B1-60C4]
    p.slot0.y = (p.y + (p.yvel >> 4) - _s16(yoff)) & 0xFFFF     # [60C7-60DB]


def _attack_spawn(out: dict, rec: int, rb, rw) -> bool:
    """1030:6017-6070 — spawn a projectile into the first free 0x4F2E slot (stride 0x12, 4 slots; free = [+4]
    ==0xFFFF). Reads the projectile's sprite/offsets from just past the phase frame-table's terminator.
    Returns True if a slot was taken (the caller then sets [0x4F0E]=0xFFFF)."""
    be = DictBackend(rb, rw, out)
    p, g = PlayerView(be), PlayerGlobals(be)
    slot = next((s for s in g.projectiles if s.free), None)    # [627C-6293] the first free 0x4F2E slot
    if slot is None:
        return False
    slot.kind = (rb((rec + 4) & 0xFFFF) >> 1) & 3              # [601C-601E] (flag>>1)&3 (al is post-shr)
    bx = rw(rec)                                               # [6021] frame-table ptr
    while rw(bx) != 0x55AA:                                    # [6025-602B] walk to the terminator
        bx = (bx + 2) & 0xFFFF
    bx = (bx + 6) & 0xFFFF                                     # [602D] past terminator -> the spawn record
    slot.spawn_ptr = bx                                        # [6030]
    sprid = rw(bx)                                             # [6033] ax
    cx = rw((bx - 4) & 0xFFFF)                                 # [6035] x offset
    yoff = rw((bx - 2) & 0xFFFF)                               # [6038]
    slot.yoff = yoff                                           # [603B]
    if p.facing_lo & 0x80:                                     # [603E-6049] facing flip
        sprid |= 0x8000
        cx = (-_s16(cx)) & 0xFFFF
    slot.sprite = sprid & 0xFFFF                               # [604B]
    slot.xoff = cx & 0xFFFF                                    # [604E]
    slot.x = (p.slot0.x + (_s16(cx) >> 4)) & 0xFFFF            # [6051-605E] pos relative to the render sprite
    slot.y = (p.slot0.y + (_s16(yoff) >> 4)) & 0xFFFF          # [6060-606D]
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
    p, g, be = _views(rb, rw)
    out = be.writes
    sfx: list = []
    state, ptr = player_set_anim(al, bx, p.anim_b, p.anim_ptr, rw)            # [5F96]
    p.anim_b = state
    frame, new_ptr, bcf = player_advance_anim(ptr, p.facing_lo, rw)           # [5F99]
    p.anim_ptr = new_ptr
    p.sprite = frame
    g.anim_hi = bcf
    p.xvel = player_friction_sym(p.xvel & 0xFFFF, p.motion_mode)              # [5F9C]
    g.idle_timer = _sat_inc_byte(g.idle_timer)                                # [5F9F]

    phase = g.attack_phase                                                    # [5FA9-5FAF]
    rec = (ATTACK_PHASE_TABLE + 5 * phase) & 0xFFFF
    v19 = rb((rec + 3) & 0xFFFF)                                              # [5FB1] phase.v19
    if g.charge != 0:                                                         # [5FB5-5FBE]
        v19 = (v19 << 2) & 0xFF
    g.attack_v19 = v19                                                        # [5FC0]
    bd0 = (~bcf) & 0x40                                                       # [5FC3-5FC8]
    g.anim_gate = bd0

    # The render-sprite/spawn read Xvel/Yvel *after* this routine's friction (and the sound path's Yvel
    # nudge) wrote them — expose pending writes through an overlay.
    def rb_ov(off):
        return (out[off] & 0xFF) if off in out else rb(off)

    def rw_ov(off):
        return (out[off] & 0xFFFF) if off in out else rw(off)

    if bd0 != 0:                                                              # [5FCD-5FCF] -> 6081 main
        _attack_render_sprite(out, rec, frame, rb_ov, rw_ov)
        return out, sfx

    # sound path [5FD2+]
    g.input_suppress = rb((rec + 2) & 0xFFFF)                                 # [5FD2] phase.sfx
    sfx.append(5 if phase == 0 else (0 if phase == 1 else 0x0A))             # [5FD9-5FEB] play_sfx dl
    a27 = out[P_ANIM_B]                                                         # [5FF0] (post set_anim)
    if a27 == 6:                                                             # [5FF3-5FF5]
        dy = 0
    elif a27 == 3:                                                           # [5FF7/5FFA-5FFC] dx=0xFFE0
        dy = (-0x20) & 0xFFFF
    else:                                                                    # [5FFE] dx=0xFFD0
        dy = (-0x30) & 0xFFFF
        trail = player_emit_trail(p.x, p.y, g.frame_blink, g.trail_ring)      # [6001] call 5E11
        if trail is not None:
            out.update(trail[0])
            g.trail_ring = trail[1]
    if g.unk_6BFE == 0:                                                       # [6004-600B]
        p.yvel = ((p.yvel & 0xFFFF) + dy) & 0xFFFF
    if (rb((rec + 4) & 0xFFFF) & 1) and _attack_spawn(out, rec, rb_ov, rw_ov):  # [600F-6017] flag bit0 + free slot
        p.slot0.sprite = 0xFFFF                                               # [6070] suppress the player draw
    elif g.fall_frames == 0:                                                  # [6075-607A]
        _attack_render_sprite(out, rec, frame, rb_ov, rw_ov)                  # [6081]
    else:
        p.slot0.sprite = 0xFFFF                                               # [607C]
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


#: DGROUP fields the F1 debug-kill path touches.
LIVES = 0x27D8               # [asm 65BA/65C1] remaining player lives
_RESPAWN_SCRATCH = 0x27D6    # [asm 65C5] cleared on a life loss (checkpoint/anim scratch)
DEATH_TIMER = 0x6BE4         # [asm 65CA/5875] the respawn timer 4C69 dispatches on ([0x6BE4]==1 -> 4F6C)
GAMEOVER_FLAG = 0x6BE5       # [asm 65D0] set at 0 lives -> 4C69 dispatches the game-over restart (5063)


def player_f1_suicide(rb) -> dict:
    """Recover the F1 debug 'kill-self' branch ``1030:5872-5879`` (call 65AF -> 65B3/65D0, then 5875 dec).

    The player update (5850) takes this branch when the respawn timer is idle ([0x6BE4]==0) AND the F1 key is
    held ([0x282F] != 0 — [0x282F] is the keyboard-held flag for scancode 0x3B=F1, the routine's ONLY reader).
    65B3: with more than one life, spend a life ([0x27D8]--), clear the respawn scratch ([0x27D6]=0) and arm the
    respawn timer ([0x6BE4]=2); at the last life instead raise the game-over flag ([0x6BE5]=1). Then 5875 always
    decrements [0x6BE4] once more (so the life-loss case lands on the =1 the level-state machine 4C69 routes to
    the respawn 4F6C; the game-over case wraps 0->0xFF, harmlessly, as [0x6BE5] takes over). The caller then
    ``jmp 5A8C`` — the frame's remaining player update (input/FSM/integrate/collision/timers) is skipped.

    Pure: reads via ``rb`` (byte reader); returns the DGROUP byte writes. Byte-exact to the ASM's read-after-write
    ordering (the 5875 dec observes 65B3's just-written timer)."""
    be = DictBackend(rb, lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8))
    g = PlayerGlobals(be)
    if g.lives == 0:                         # [asm 65BA/65BF -> 65D0]
        g.end_signal = 1
        timer = g.respawn_state              # 65D0 leaves the timer untouched (==0 here)
    else:                                    # [asm 65C1-65CA]
        g.lives = g.lives - 1
        g.energy = 0
        timer = 2
    g.respawn_state = (timer - 1) & 0xFF     # [asm 5875] dec after the call (reads 65B3's write)
    return be.writes


def player_tick_timers(timers: dict) -> dict:
    """Recover the player-update timer tail ``1030:5A47..5A87``.

    Decrement each of the seven byte countdown timers + the one word countdown timer, every one clamped at 0.
    Pure: ``timers`` maps each address in ``TIMER_BYTES``/``TIMER_WORD`` to its current value; returns the new
    values (same keys). Bytes are 8-bit-wrapped, the word is 16-bit."""
    out = {a: _dec_floor(timers[a], 8) for a in TIMER_BYTES}     # [5A4A-5A7E] seven byte timers
    out[TIMER_WORD] = _dec_floor(timers[TIMER_WORD], 16)         # [5A82-5A87] one word timer
    return out
