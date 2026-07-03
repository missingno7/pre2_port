"""The native (VM-less) per-frame player update — 1030:5850, composed over a NativeGameState.

5850 is the whole player pass: a register-saving wrapper around input-decode -> FSM -> X/Y integrate ->
ground/tile collision -> timers. Every gameplay sub-part is already a verified pure function (decode_input,
player_fsm_step, player_x_integrate, player_y_integrate, collision, player_tick_timers); this composes them in
the ASM's order over the NativeGameState in place, exactly as the live hybrid hooks do over VM memory — so each
sub-step reads the previous one's writes (the FSM's new Xvel feeds the X integrate, DC1's flags feed the FSM).

The non-gameplay sub-calls are intentionally NOT run: 454E (player-sprite VRAM bg-save; its only DGROUP writes
are the [0x6CA2..0x6CA6] render slot) and 6294 ([0x280D] vblank spin) are the renderer's / timing's job. The
dormant branches fail loud rather than silently fall back to ASM: the death/respawn path (65AF), the pause spin
([0x2830]), the cheat-combo action (247B -> 2505), and active momentum (484E when [0x6BC5]!=0). None fire in
normal play; the native frame reaching one means an assumption broke, and it says so.

Verified byte-exact (DGROUP) vs the ASM 5850->5A95 over the demos: pre2/probes/probe_native_player_step.py.
"""
from __future__ import annotations

from pre2.bridge.input_decode import apply_ds, readers
from pre2.gaps import Pre2HybridGap
from pre2.native.state import DATA_SEG
from pre2.recovered.input_decode import Pre2InputGap, decode_input
from pre2.recovered.object_inject import find_free_object_slot
from pre2.recovered.player import (FSM_WORD_FIELDS, TIMER_BYTES, TIMER_WORD, player_flying_484e, player_fsm_step,
                                   player_tick_timers, player_x_integrate, player_y_integrate)
from pre2.recovered.player_collision import collision
from pre2.recovered.player_interaction import player_interaction_tick

_PX, _PY = 0x4F1C, 0x4F1E
_XVEL, _YVEL, _CAM_LEFT = 0x4F22, 0x4F2A, 0x8164
_RENDER_SUPPRESS = 0x4F0E       # [asm 585E] =0xFFFF: hide the player sprite until the FSM/handlers place it
_DEATH_TIMER, _DEATH_FLAG = 0x6BE4, 0x282F
_PAUSE_FLAG = 0x2830
_MOMENTUM_GATE = 0x6BC5         # 484E dormant when 0
_MAP_SEG_PTR = 0x2DDA

#: 454E's only DGROUP-0x1A0F writes (the render bg-save slot) — render state, not the player's gameplay contract.
RENDER_OFFSETS = frozenset((0x6CA2, 0x6CA3, 0x6CA4, 0x6CA5, 0x6CA6))

_BASE = (DATA_SEG << 4) & 0xFFFFF


def _w(state, off: int, val: int, width: int) -> None:
    for k in range(width):
        state.data[(_BASE + ((off + k) & 0xFFFF)) & 0xFFFFF] = (val >> (8 * k)) & 0xFF


def _keycombo_active(rb) -> bool:
    """247B's arm gate: all three combo flags set (the common case is inactive -> the routine is a read-only
    no-op). [asm 247B-2486: al = [0x2811] & [0x282C] & [0x2805]; je return]"""
    return rb(0x2811) != 0 and rb(0x282C) != 0 and rb(0x2805) != 0


def native_player_step(state) -> None:
    """Run the whole 5850 player update over the NativeGameState in place (no VM)."""
    rb, rw = readers(state)
    _w(state, _RENDER_SUPPRESS, 0xFFFF, 2)                              # [asm 585E]

    if rb(_DEATH_TIMER) == 0 and rb(_DEATH_FLAG) != 0:                  # [asm 5864-5879]
        raise Pre2HybridGap("native player: death/respawn path (65AF) not recovered")
    if rb(_PAUSE_FLAG) != 0:                                            # [asm 587C-588F]
        raise Pre2HybridGap("native player: pause spin ([0x2830]) not handled")
    if _keycombo_active(rb):                                            # [asm 5892 -> 247B]
        raise Pre2HybridGap("native player: cheat-combo action (247B->2505) not recovered")

    # [asm 5897/58A1] 454E render bg-save + 6294 vblank spin -> not gameplay state, skipped.
    try:
        apply_ds(state, decode_input(rb, rw))                          # [asm 58A4] DC1 input decode
    except Pre2InputGap as exc:
        raise Pre2HybridGap(f"native player: {exc}") from exc

    writes, sfx, scroll = player_fsm_step(rb, rw)                       # [asm 58A7] FSM (sfx = jump/land/... sounds)
    for a, v in writes.items():
        _w(state, a, v, 2 if a in FSM_WORD_FIELDS else 1)
    from pre2.native.audio import native_emit_sfx
    native_emit_sfx(state, sfx)                                        # emit the FSM's play_sfx commands
    if scroll:                                                         # idle look-around pan (anim13)
        from pre2.bridge.camera_pan import apply_camera_pan
        apply_camera_pan(state, scroll)

    _w(state, _PX, player_x_integrate(rw(_PX), rw(_XVEL), rw(_CAM_LEFT)), 2)   # [asm 5A0F]
    _w(state, _PY, player_y_integrate(rw(_PY), rw(_YVEL)), 2)                  # [asm 5A36]

    es_base = (rw(_MAP_SEG_PTR) << 4) & 0xFFFFF                         # [asm 5A41] ground/tile collision
    ds_w, map_w, _redraws = collision(rb, rw, lambda o: state.data[(es_base + (o & 0xFFFF)) & 0xFFFFF])
    for a, v in ds_w.items():
        _w(state, a, v, 1)
    for o, v in map_w.items():
        state.data[(es_base + (o & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    if rb(_MOMENTUM_GATE) != 0:                                         # [asm 5A44 -> 484E] the glider/flying update
        for a, (v, wd) in player_flying_484e(rb, rw).items():          # anim [0x4F20] + tilt [0x7B1A] + wing slot
            _w(state, a, v, wd)

    timers = {a: rb(a) for a in TIMER_BYTES}                           # [asm 5A47-5A8B]
    timers[TIMER_WORD] = rw(TIMER_WORD)
    out = player_tick_timers(timers)
    for a in TIMER_BYTES:
        _w(state, a, out[a], 1)
    _w(state, TIMER_WORD, out[TIMER_WORD], 2)


def native_player_interaction(state) -> None:
    """The whole player<->world interaction pass (1030:8295), composed over NativeGameState.

    Runs the recovered :func:`player_interaction_tick` (loop1 = player-vs-enemy stomp/hurt/die, loop2 = the
    ~25 pickups) in place: it applies its own writes mid-pass (loop1's results feed loop2's reads), allocates
    object slots via find_free_object_slot, and emits hit/pickup sfx (hurt=3, die=9, ...) via native_play_sfx.
    Every effect path is recovered byte-exact, so — like the live hook — there is no fail-loud branch to guard."""
    from pre2.native.audio import native_play_sfx
    rb, rw = readers(state)
    read_id = lambda slot: rw((0x4FD0 + slot * 0x12 + 4) & 0xFFFF)        # noqa: E731 — object render-id reader
    player_interaction_tick(rb, rw, lambda w: apply_ds(state, w), lambda dl: native_play_sfx(state, dl),
                            lambda: find_free_object_slot(read_id))
