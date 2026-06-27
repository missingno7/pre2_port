"""Checkpoint for the player horizontal kinematics (1030:5A0F..5A33) — first live player-FSM leaf.

This is an INLINE block inside the per-frame player update (no CALL/RET), so the hook does the recovered
integrate, writes ``[0x4F1C]`` back (only when the move commits), then JUMPS to 0x5A36 (the Y-integrate). Like
the object ``object_velocity`` block it reproduces the ASM's architectural side effects (final FLAGS via the
last ``cmp`` + the per-path instruction count) so it stays as transparent as an atomic block-swap can be.

(Demo byte-determinism is already affected upstream by the live ``object_tick`` collapse; this hook is verified
the desync-immune way — per-call shadow in verify mode + the hook-audit firing count.)

In verify mode the ASM is the oracle: the hook predicts (no mutation) and the verify-exit hook at 0x5A36
diffs the recovered ``[0x4F1C]`` against the ASM's.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.player import (
    FSM_WORD_FIELDS, TIMER_BYTES, TIMER_WORD, VIEW_TILES, X_MAX, X_MIN, _s16,
    player_fsm_step, player_tick_timers, player_x_integrate, player_y_integrate,
)

from .common import Pre2HybridGap, report

_FSM_ENTRY = (0x1030, 0x58A7)  # after the input-decode call (DC1); start of the FSM front-end
_FSM_EXIT = (0x1030, 0x5A0F)   # == the X-integrate entry: the FSM handler dispatch (5A0B) returns here
_PLAY_SFX = (0x1030, 0x0282)   # play_sfx(dl): the audio command the attack handler emits
_ENTRY = (0x1030, 0x5A0F)     # mov ax,[0x4F22]  (start of the X integrate)
_NEXT = (0x1030, 0x5A36)      # mov ax,[0x4F2A]  (the Y integrate — first instruction after the X block)
_ENTRY_Y = (0x1030, 0x5A36)   # mov ax,[0x4F2A]  (start of the Y integrate)
_NEXT_Y = (0x1030, 0x5A41)    # call 0x5A96      (the ground/tile collision — first instruction after Y)
_ENTRY_T = (0x1030, 0x5A47)   # mov dx,1         (start of the per-frame timer decrements)
_NEXT_T = (0x1030, 0x5A8C)    # pop bp           (the routine epilogue — first instruction after the timers)
_DS = 0x1A0F
_PX = 0x4F1C
_PY = 0x4F1E

# The FSM write-contract window (the player struct + the FSM scratch flags + the projectile/trail rings). Diffed
# for completeness in verify mode: every byte that changed must be one player_fsm_step predicted.
_FSM_WATCH = (tuple(range(0x4F00, 0x4F30)) + tuple(range(0x4F74, 0x4FC2))
              + (0x6BC8, 0x6BCF, 0x6BD1, 0x6BD3, 0x6BE1, 0x6BCE, 0x6BBE, 0x6BBF, 0x6BFE,
                 0x6BDB, 0x6BDC, 0x6BEB, 0x6BEC, 0x4F1B, 0x6BCD, 0x6BD0))


def _rb(mem, off):
    return mem.data[((_DS << 4) + off) & 0xFFFFF]


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _wb(mem, off, v):
    mem.data[((_DS << 4) + off) & 0xFFFFF] = v & 0xFF


def _ww(mem, off, v):
    b = ((_DS << 4) + off) & 0xFFFFF
    mem.data[b] = v & 0xFF
    mem.data[b + 1] = (v >> 8) & 0xFF


def _apply_fsm_writes(mem, writes) -> None:
    """Apply player_fsm_step's write dict to DS memory at the right width (FSM_WORD_FIELDS = words)."""
    for a, v in writes.items():
        base = ((_DS << 4) + (a & 0xFFFF)) & 0xFFFFF
        mem.data[base] = v & 0xFF
        if a in FSM_WORD_FIELDS:
            mem.data[(base + 1) & 0xFFFFF] = (v >> 8) & 0xFF


def _emit_sfx(cpu, sfx) -> None:
    """Preserve the attack handler's sound by invoking play_sfx(dl) (1030:0282) as a controlled near-call: the
    audio observer hooked at its entry then emits the event and the game's audio state updates. No-op when the
    routine isn't present (e.g. silent demo replay)."""
    for dl in sfx:
        save_ip = cpu.s.ip & 0xFFFF
        cpu.s.dx = (cpu.s.dx & 0xFF00) | (dl & 0xFF)
        cpu.push(save_ip)
        sp_target = cpu.s.sp
        cpu.s.ip = _PLAY_SFX[1]
        guard = 0
        while not (cpu.s.sp == ((sp_target + 2) & 0xFFFF) and (cpu.s.cs & 0xFFFF) == _PLAY_SFX[0]
                   and (cpu.s.ip & 0xFFFF) == save_ip):
            cpu.step()
            guard += 1
            if guard > 200_000:
                raise Pre2HybridGap("play_sfx did not return")


@registry.replace(*_FSM_ENTRY, "player_fsm")
def player_fsm_hook(cpu) -> None:
    """Native replacement for the per-frame player FSM at 1030:58A7..5A0B (front-end -> select -> dispatch)."""
    mem = cpu.mem
    if _rb(mem, 0x6BC5) != 0:                       # the dormant [0x6BC5]!=0 momentum path is unrecovered
        if getattr(cpu, "pre2_verify_mode", False):
            cpu.pre2_fsm_pending.append(None)       # skip the diff this frame (ASM runs the momentum path)
            interpret_current_instruction_without_hook(cpu)
            return
        raise Pre2HybridGap("player FSM momentum path ([0x6BC5]!=0) not recovered")

    rb = lambda o: _rb(mem, o)
    rw = lambda o: _rw(mem, o)

    if getattr(cpu, "pre2_verify_mode", False):
        try:
            writes, _sfx, _scroll = player_fsm_step(rb, rw)   # the ASM oracle does the look-around pan itself
        except NotImplementedError as exc:
            cpu.pre2_fsm_pending.append(("gap", str(exc)))
            interpret_current_instruction_without_hook(cpu)
            return
        entry = {a: _rb(mem, a) for a in _FSM_WATCH}
        cpu.pre2_fsm_pending.append(("writes", writes, entry))
        interpret_current_instruction_without_hook(cpu)
        return

    try:
        writes, sfx, scroll = player_fsm_step(rb, rw)
    except NotImplementedError as exc:
        raise Pre2HybridGap(f"player FSM (58A7): {exc}") from exc
    _apply_fsm_writes(mem, writes)
    cpu.s.ip = _FSM_EXIT[1]                          # jump to the X integrate (skip the ASM FSM body)
    if scroll:                                       # idle look-around (anim13): pan + reveal the column
        from pre2.bridge.camera_pan import apply_camera_pan
        apply_camera_pan(mem, scroll)
    if sfx:
        _emit_sfx(cpu, sfx)


def _diff_fsm(cpu) -> None:
    """At the FSM exit (5A0F), diff player_fsm_step's prediction (from 58A7) against the ASM over the watch
    window. ``cpu.pre2_fsm_verify`` carries the (stats, on_result, raise) reporter config."""
    pending = getattr(cpu, "pre2_fsm_pending", None)
    if not pending:
        return
    rec = pending.pop()
    cfg = getattr(cpu, "pre2_fsm_verify", None)
    if cfg is None or rec is None or rec[0] is None:    # not verifying, or [0x6BC5]!=0 skip frame
        return
    stats, on_result, raise_on_div = cfg
    if rec[0] == "gap":
        report(stats, on_result, raise_on_div, "player_fsm", f"gap: {rec[1]}")
        return
    _, writes, entry = rec
    mem = cpu.mem
    pred = {}
    for a, v in writes.items():
        pred[a] = v & 0xFF
        if a in FSM_WORD_FIELDS:
            pred[(a + 1) & 0xFFFF] = (v >> 8) & 0xFF
    # the render-sprite position bytes are don't-care when the sprite is suppressed ([0x4F0E]==0xFFFF)
    dead = {0x4F0A, 0x4F0B, 0x4F0C, 0x4F0D} if _rw(mem, 0x4F0E) == 0xFFFF else set()
    reason = None
    for a in _FSM_WATCH:
        if a in dead:
            continue
        want = pred.get(a, entry.get(a, _rb(mem, a)))
        got = _rb(mem, a)
        if got != want:
            reason = f"[{a:#06x}] rec={want:#04x} asm={got:#04x}"
            break
    report(stats, on_result, raise_on_div, "player_fsm", reason)


@registry.replace(*_ENTRY, "player_x_integrate")
def player_x_integrate_hook(cpu) -> None:
    """Native replacement for the player horizontal kinematics at 1030:5A0F..5A33.

    5A0F is also the FSM exit, so in verify mode this hook first diffs the FSM prediction (made at 58A7, now all
    committed by the ASM) before predicting X."""
    mem = cpu.mem
    x, xvel, cam_left = _rw(mem, _PX), _rw(mem, 0x4F22), _rw(mem, 0x8164)
    new_x = player_x_integrate(x, xvel, cam_left)

    if getattr(cpu, "pre2_verify_mode", False):
        _diff_fsm(cpu)
        cpu.pre2_player_pending.append(new_x)
        interpret_current_instruction_without_hook(cpu)
        return

    # Reproduce the block's regs/FLAGS/instruction-count by the ASM control-flow path (see the disasm at
    # 5A25/5A2C/5A31): the next IRQ pushes FLAGS, and the block runs 10/12/14/15 ASM insns; step() adds 1.
    nx = (x + (_s16(xvel) >> 4)) & 0xFFFF
    bound = ((cam_left + VIEW_TILES) << 4) & 0xFFFF
    if _s16(bound) <= _s16(nx):                                  # [5A27 jle] blocked at the camera edge
        cpu.set_sub_flags(bound, nx, (bound - nx) & 0xFFFF, 16)
        cpu.instruction_count += 9
    elif _s16(nx) < X_MIN:                                       # [5A2C jl] blocked at the left world edge
        cpu.set_sub_flags(nx, X_MIN, (nx - X_MIN) & 0xFFFF, 16)
        cpu.instruction_count += 11
    elif _s16(nx) >= X_MAX:                                      # [5A31 jge] blocked at the right world edge
        cpu.set_sub_flags(nx, X_MAX, (nx - X_MAX) & 0xFFFF, 16)
        cpu.instruction_count += 13
    else:                                                       # [5A33] commit (mov keeps the 5A2E cmp flags)
        b = ((_DS << 4) + _PX) & 0xFFFFF
        mem.data[b] = nx & 0xFF
        mem.data[b + 1] = (nx >> 8) & 0xFF
        cpu.set_sub_flags(nx, X_MAX, (nx - X_MAX) & 0xFFFF, 16)
        cpu.instruction_count += 14
    cpu.s.ip = _NEXT[1]


@registry.replace(*_ENTRY_Y, "player_y_integrate")
def player_y_integrate_hook(cpu) -> None:
    """Native replacement for the player vertical kinematics at 1030:5A36..5A3D (Y += sar(Yvel,4))."""
    mem = cpu.mem
    y, yvel = _rw(mem, _PY), _rw(mem, 0x4F2A)
    new_y = player_y_integrate(y, yvel)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_player_y_pending.append(new_y)
        interpret_current_instruction_without_hook(cpu)
        return

    # The block is 4 ASM insns ending in `add [0x4F1E],ax`; step() adds 1, so add 3. Reproduce the add's FLAGS
    # (the next IRQ pushes them).
    b = ((_DS << 4) + _PY) & 0xFFFFF
    mem.data[b] = new_y & 0xFF
    mem.data[b + 1] = (new_y >> 8) & 0xFF
    cpu.set_add_flags(y, (_s16(yvel) >> 4) & 0xFFFF, new_y, 16)
    cpu.instruction_count += 3
    cpu.s.ip = _NEXT_Y[1]


@registry.replace(*_ENTRY_T, "player_tick_timers")
def player_tick_timers_hook(cpu) -> None:
    """Native replacement for the player-update timer tail at 1030:5A47..5A87 (8 saturating countdowns)."""
    mem = cpu.mem
    timers = {a: _rb(mem, a) for a in TIMER_BYTES}
    timers[TIMER_WORD] = _rw(mem, TIMER_WORD)
    out = player_tick_timers(timers)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_player_t_pending.append(out)
        interpret_current_instruction_without_hook(cpu)
        return

    for a in TIMER_BYTES:
        _wb(mem, a, out[a])
    _ww(mem, TIMER_WORD, out[TIMER_WORD])
    # Straight-line block, 17 ASM insns; step() adds 1 -> add 16. Reproduce the final `adc word,0` FLAGS
    # (== add of the post-`sub` value + its borrow) for IRQ-push transparency; they are otherwise dead (the
    # next instruction is `pop bp`).
    orig = timers[TIMER_WORD]
    sub_res = (orig - 1) & 0xFFFF
    cpu.set_add_flags(sub_res, 1 if orig == 0 else 0, out[TIMER_WORD], 16)
    cpu.instruction_count += 16
    cpu.s.ip = _NEXT_T[1]


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hooks: diff the recovered FSM (5A0F) / X (5A36) / Y (5A41) / timers (5A8C) vs
    the ASM. The FSM diff runs inside the X-integrate hook at the shared 5A0F boundary (see _diff_fsm)."""
    cpu.pre2_fsm_verify = (stats, on_result, raise_on_divergence)

    def _verify_x_at_next(c) -> None:
        # 0x5A36 is the X-integrate EXIT *and* the Y-integrate ENTRY. In verify mode this hook shadows the
        # player_y_integrate replacement at the same address, so it does both jobs: diff X (the prediction
        # made at 5A0F) and capture the Y prediction (5A0F always runs immediately before 5A36).
        pending = getattr(c, "pre2_player_pending", None)
        if pending:
            pred = pending.pop()
            actual = _rw(c.mem, _PX)
            reason = None if pred == actual else f"X rec={pred:#06x} asm={actual:#06x}"
            report(stats, on_result, raise_on_divergence, "player_x_integrate", reason)
        c.pre2_player_y_pending.append(player_y_integrate(_rw(c.mem, _PY), _rw(c.mem, 0x4F2A)))
        interpret_current_instruction_without_hook(c)

    def _verify_y_at_next(c) -> None:
        pending = getattr(c, "pre2_player_y_pending", None)
        if pending:
            pred = pending.pop()
            actual = _rw(c.mem, _PY)
            reason = None if pred == actual else f"Y rec={pred:#06x} asm={actual:#06x}"
            report(stats, on_result, raise_on_divergence, "player_y_integrate", reason)
        interpret_current_instruction_without_hook(c)

    def _verify_t_at_next(c) -> None:
        pending = getattr(c, "pre2_player_t_pending", None)
        if pending:
            pred = pending.pop()
            bad = [hex(a) for a in pred if pred[a] != (_rw(c.mem, a) if a == TIMER_WORD else _rb(c.mem, a))]
            reason = None if not bad else f"timers differ at {bad}"
            report(stats, on_result, raise_on_divergence, "player_tick_timers", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_NEXT] = _verify_x_at_next
    cpu.hook_names[_NEXT] = "player_x_integrate_verify"
    cpu.replacement_hooks[_NEXT_Y] = _verify_y_at_next
    cpu.hook_names[_NEXT_Y] = "player_y_integrate_verify"
    cpu.replacement_hooks[_NEXT_T] = _verify_t_at_next
    cpu.hook_names[_NEXT_T] = "player_tick_timers_verify"
