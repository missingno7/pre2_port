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

from pre2.views.memory_adapter import apply_ds, readers
from pre2.gaps import Pre2CheatCredits, Pre2HybridGap
from pre2.native.state import DATA_SEG
from pre2.recovered.input_decode import Pre2InputGap, decode_input
from pre2.recovered.object_inject import find_free_object_slot
from pre2.recovered.player import (FSM_WORD_FIELDS, TIMER_BYTES, TIMER_WORD, player_f1_suicide, player_flying_484e,
                                   player_fsm_step, player_tick_timers, player_x_integrate, player_y_integrate)
from pre2.recovered.player_collision import collision
from pre2.recovered.player_interaction import player_interaction_tick
from pre2.views.dgroup_view import PlayerGlobals, PlayerView, apply_contract
from pre2.native.dgroup_offsets import (INPUT_READY_A, INPUT_READY_B, INPUT_READY_C, KEY_TABLE)

_PX, _PY = 0x4F1C, 0x4F1E
_XVEL, _YVEL, _CAM_LEFT = 0x4F22, 0x4F2A, 0x8164
_RENDER_SUPPRESS = 0x4F0E       # [asm 585E] =0xFFFF: hide the player sprite until the FSM/handlers place it
_DEATH_TIMER = 0x6BE4           # [asm 5864] respawn timer; idle (==0) except mid death/respawn
_F1_KEY = 0x282F                # [asm 586B] keyboard-held flag for scancode 0x3B = F1 (the debug kill-self key)
_F2_KEY = 0x2830                # [asm 587C] keyboard-held flag for scancode 0x3C = F2 (the debug abort->game-over key)
_GAMEOVER_FLAG = 0x6BE5         # [asm 5883] set -> 4C69 dispatches the game-over restart (5063)
_MOMENTUM_GATE = 0x6BC5         # 484E dormant when 0
_MAP_SEG_PTR = 0x2DDA

#: 454E's only DGROUP-0x1A0F writes (the render bg-save slot) — render state, not the player's gameplay contract.
RENDER_OFFSETS = frozenset((0x6CA2, 0x6CA3, 0x6CA4, 0x6CA5, 0x6CA6))

_BASE = (DATA_SEG << 4) & 0xFFFFF


def _w(state, off: int, val: int, width: int) -> None:
    for k in range(width):                               # through the backend seam (Phase 4)
        state.wb((off + k) & 0xFFFF, (val >> (8 * k)) & 0xFF)


def _keycombo_active(rb) -> bool:
    """247B's arm gate: all three combo flags set (the common case is inactive -> the routine is a read-only
    no-op). [asm 247B-2486: al = [0x2811] & [0x282C] & [0x2805]; je return]"""
    return rb(INPUT_READY_B) != 0 and rb(INPUT_READY_C) != 0 and rb(INPUT_READY_A) != 0


def _combo_confirmed(rb, third_sc: int) -> bool:
    """A Ctrl+Alt+<key> Easter-egg combo: Left-Ctrl (0x1D) + Left-Alt (0x38) + a third scancode held AND NO OTHER
    key pressed — the shared [asm 2488-24A7 / 25D9-25F2] shape: scan every scancode flag [0x27F4+sc] for sc in
    1..0x7E, skipping the three combo scancodes, and abort if any other is down. ``third_sc``: 0x11 (W/Z, the 247B
    dev-credits combo) or 0x12 (E, the 25C7 game-over creators-photo combo). The third-key DGROUP flag is at
    [0x27F4 + third_sc]. Returns True only on an exact match, else the routine is a no-op."""
    if rb(INPUT_READY_B) == 0 or rb(INPUT_READY_C) == 0 or rb(KEY_TABLE + third_sc) == 0:   # Ctrl & Alt & third key all held
        return False
    for sc in range(1, 0x7F):
        if sc in (0x1D, 0x38, third_sc):
            continue
        if rb(KEY_TABLE + sc) != 0:
            return False
    return True


def _cheat_combo_confirmed(rb) -> bool:
    """The full 247B trigger: Ctrl+Alt+W/Z (scancode 0x11) with no other key -> the dev-credits screen (2505)."""
    return _combo_confirmed(rb, 0x11)


def ecombo_confirmed(state) -> bool:
    """The 25C7 game-over trigger: Ctrl+Alt+E (scancode 0x12) with no other key -> the creators photo (25F6).
    Checked at the top of the game-over routine (506c), so the flow driver tests it as the GAME OVER scene begins."""
    rb, _rw = readers(state)
    return _combo_confirmed(rb, 0x12)


def native_player_step(state) -> None:
    """Run the whole 5850 player update over the NativeGameState in place (no VM)."""
    rb, rw = readers(state)
    pv, g = PlayerView(state), PlayerGlobals(state)
    pv.slot0.sprite = 0xFFFF                                            # [asm 585E] suppress the raw player draw

    if g.respawn_state == 0 and rb(_F1_KEY) != 0:                       # [asm 5864-5879] F1 debug kill-self
        apply_contract(state, player_f1_suicide(rb))                   #   65AF/65B3: spend a life (or game-over)
        return                                                         #   [asm 5879] jmp 5A8C — skip the rest
    if rb(_F2_KEY) != 0:                                                # [asm 587C-588F] F2 debug abort -> game over
        g.end_signal = 1                                                #   [asm 5883] set [0x6BE5]=1 (4C69 -> 5063)
        return                                                          #   [asm 588F] jmp 5A8C — skip the rest
    if _keycombo_active(rb) and _cheat_combo_confirmed(rb):             # [asm 5892 -> 247B] the dev-credits combo
        raise Pre2CheatCredits()                                        #   Ctrl+Alt+W(/Z), no other key -> 2505

    # [asm 5897/58A1] 454E render bg-save + 6294 P-pause busy-wait ([0x280D]) -> no gameplay-state writes,
    # skipped here; the pause freeze is a presentation concern driven by play_native's gameplay loop.
    try:
        apply_ds(state, decode_input(rb, rw))                          # [asm 58A4] DC1 input decode
    except Pre2InputGap as exc:
        raise Pre2HybridGap(f"native player: {exc}") from exc

    writes, sfx, scroll = player_fsm_step(rb, rw)                       # [asm 58A7] FSM (sfx = jump/land/... sounds)
    apply_contract(state, writes, word_fields=FSM_WORD_FIELDS)
    from pre2.native.audio import native_emit_sfx, player_sfx_x
    native_emit_sfx(state, sfx, player_sfx_x(state))                   # emit the FSM's play_sfx commands (jump/throw)
    if scroll:                                                         # idle look-around pan (anim13)
        from pre2.views.camera_pan import apply_camera_pan
        apply_camera_pan(state, scroll)

    pv.x = player_x_integrate(pv.x, pv.xvel & 0xFFFF, g.cam_left)      # [asm 5A0F]
    pv.y = player_y_integrate(pv.y, pv.yvel & 0xFFFF)                   # [asm 5A36]

    es_base = (rw(_MAP_SEG_PTR) << 4) & 0xFFFFF                         # [asm 5A41] ground/tile collision
    ds_w, map_w, _redraws = collision(rb, rw, lambda o: state.data[(es_base + (o & 0xFFFF)) & 0xFFFFF])
    apply_contract(state, ds_w)                                         # byte-level overlay contract
    for o, v in map_w.items():
        state.data[(es_base + (o & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    if rb(_MOMENTUM_GATE) != 0:                                         # [asm 5A44 -> 484E] the glider/flying update
        apply_contract(state, player_flying_484e(rb, rw))              # anim + tilt + the wing slot (width tuples)

    timers = {a: rb(a) for a in TIMER_BYTES}                           # [asm 5A47-5A8B]
    timers[TIMER_WORD] = rw(TIMER_WORD)
    out = player_tick_timers(timers)
    apply_contract(state, {**{a: out[a] for a in TIMER_BYTES}, TIMER_WORD: (out[TIMER_WORD], 2)})


def native_player_interaction(state) -> None:
    """The whole player<->world interaction pass (1030:8295), composed over NativeGameState.

    Runs the recovered :func:`player_interaction_tick` (loop1 = player-vs-enemy stomp/hurt/die, loop2 = the
    ~25 pickups) in place: it applies its own writes mid-pass (loop1's results feed loop2's reads), allocates
    object slots via find_free_object_slot, and emits hit/pickup sfx (hurt=3, die=9, ...) via native_play_sfx.
    Every effect path is recovered byte-exact, so — like the live hook — there is no fail-loud branch to guard."""
    from pre2.native.audio import native_play_sfx, player_sfx_x
    rb, rw = readers(state)
    read_id = lambda slot: rw((0x4FD0 + slot * 0x12 + 4) & 0xFFFF)        # noqa: E731 — object render-id reader
    # stomp / hurt / pickups all happen AT the player (jumping on / touching the object), so pan them to the
    # player's on-screen X — a good stand-in for the co-located enemy/item without threading each object out.
    psx = player_sfx_x(state)
    player_interaction_tick(rb, rw, lambda w: apply_ds(state, w), lambda dl: native_play_sfx(state, dl, psx),
                            lambda: find_free_object_slot(read_id))
