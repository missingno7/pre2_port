"""Recovered VGA retrace-wait timing primitive (default deterministic stepper) — VM-contact layer.

A drop-in faster variant of ``scripts/play._advance_demo_frame`` for the *deterministic* paths (headless
replay, in-view demo replay, verify/oracle). It keeps that function's IRQ-delivery cadence **exactly** — the
same ``sub_batch`` boundaries, the same per-boundary PIT/SB/PIC pump — and only collapses the classified VGA
retrace busy-waits (1030:9900 / 990D / 44CD) *between* boundaries, where the interpreted model delivers no
IRQ anyway. Because the sub_batch boundaries, the clock value sampled at each, and the per-boundary pump are
identical, and because a skipped run of poll iterations is provably register/flag/memory-identical at the
boundary, the result is **byte-equivalent to the interpreted ``_advance_demo_frame``** — NOT a new timeline.
(Proven by ``pre2/probes/verify_fast_retrace.py`` + ``tests/test_timing_fastforward.py``; see the canonical-
model decision in ``docs/pre2/timing_hook_design.md`` §9.)

This is a recovered timing hook like any other island: ``scripts/play._advance_frame_deterministic`` calls it
by default with the hybrid runtime, and falls back to the interpreted ASM loops under ``--no-replacements``
or ``--no-fast-retrace-waits`` (the original timing path kept available for comparison).

Scope: deterministic clock only (``time_source(ic) = base + ic/det_speed``). Live ``--view`` wall-clock
pacing is OUT of scope (it would need an outer-loop scheduler change — see the design note §7). Pure timing —
no renderer/audio-mixer logic; the only side effects are the very instructions the interpreted loop runs.
"""
from __future__ import annotations

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from pre2.recovered.vga_timing import ALL_NODES

_REFRESH_HZ = 70.0          # VGA display refresh; matches dos.display_refresh_hz()
_CS = 0x1030

# Poll-loop back-edges: branch_ip -> (loop_top_ip, continue_bit). Taking this conditional jump loops back to
# `loop_top` precisely while the just-sampled retrace bit == continue_bit, so ON ARRIVAL the flags are
# `test(continue_bit)` (fresh and correct) — the only state where it is safe to bulk-skip further identical
# iterations (each leaves ip/registers/flags unchanged, only the deterministic clock moves).
_POLL_BACKEDGE = {
    0x9908: (0x9905, False),   # je  0x9905 — 9900 / 990D-tail CLEAR-poll: loop while CLEAR
    0x9915: (0x9912, True),    # jne 0x9912 — 990D loopA: loop while SET (falling-edge wait)
    0x44DF: (0x44DC, True),    # jne 0x44DC — 44CD loopA: loop while SET
    0x44E4: (0x44E1, False),   # je  0x44E1 — 44CD loopB: loop while CLEAR
}

# --- The 1C6F PIT frame-limiter wait (the game's ~23Hz gate: spin until 3 PIT ticks since the last frame) ---
# 1c6f mov ax,[0x27EE] / 1c72 sub ax,cs:[0x1D67] / 1c77 jns 1c7b / (1c79 neg ax) / 1c7b cmp ax,3 /
# 1c7e jb 1c6f  — 5 instructions per iteration (6 when the delta is negative). The condition source
# [DS:0x27EE] is written ONLY by the PIT ISR, and IRQs are delivered exclusively at sub_batch boundaries
# (`_pump`) — so WITHIN a segment the delta is CONSTANT and every iteration is register/flag/memory-identical
# (ax + flags are recomputed from the same values each pass). While |delta| < 3 the loop cannot exit inside
# the segment: bulk-skip whole iterations to the step budget, exactly like the retrace polls. This wait — not
# the retrace — is where a hook-collapsed frame's surplus budget burns (~84% of all interpreted instructions
# in --safe-hooks gameplay), so collapsing it is the difference between slide-show and fluent recording.
_PIT_NODES = frozenset({0x1C6F, 0x1C72, 0x1C77, 0x1C79, 0x1C7B, 0x1C7E})
_PIT_TOP = 0x1C6F
_PIT_BACKEDGE = 0x1C7E          # jb 0x1c6f — taken while |[0x27EE] - cs:[0x1D67]| < 3
_FF_NODES = frozenset(ALL_NODES) | _PIT_NODES


def _pit_wait_delta(cpu):
    """The 1C6F loop's live condition: signed [DS:0x27EE] - cs:[0x1D67] (constant within a segment)."""
    d = cpu.mem.data
    ds_base = (0x1A0F << 4)
    cs_base = (_CS << 4)
    cur = d[ds_base + 0x27EE] | (d[ds_base + 0x27EF] << 8)
    ref = d[cs_base + 0x1D67] | (d[cs_base + 0x1D68] << 8)
    v = (cur - ref) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def make_sample(det_speed, base, active_fraction):
    """Return ``sample(ic)`` reproducing ``dos._vga_status``'s SET test at ``instruction_count == ic`` under
    the deterministic clock ``time_source(ic) = base + ic/det_speed`` (``phase = (ts*70) % 1`` is SET iff
    ``phase >= 1 - active_fraction``). ``active_fraction`` MUST equal ``dos.vga_retrace_active_fraction``."""
    thr = 1.0 - active_fraction

    def sample(ic):
        return (((base + ic / det_speed) * _REFRESH_HZ) % 1.0) >= thr
    return sample


def _pump(rt, *, now, pic, sound_blaster, timer_irq, input_irq_steps, tick_state):
    """The pump half of ``play._pump_and_step`` (raise due PIT ticks against ``now``, service the SB, deliver
    pending IF-gated IRQs) — replicated verbatim so the fast path's per-boundary IRQ cadence is identical."""
    if timer_irq:
        tick_period = 1.0 / max(1.0, rt.dos.pit_channel0_hz())
        while now >= tick_state["next"]:
            if pic is not None:
                pic.raise_irq(0)
            elif rt.cpu.get_flag(IF):
                deliver_interrupt(rt, 0x08, max_steps=input_irq_steps)
            tick_state["next"] += tick_period
            if tick_state["next"] < now - 0.25:          # fell far behind: resync
                tick_state["next"] = now + tick_period
    if sound_blaster is not None:
        sound_blaster.service()
    if pic is not None:                                  # deliver pending IRQs (IF-gated)
        guard = 0
        while rt.cpu.get_flag(IF) and guard < 64:
            n = pic.acknowledge()
            if n is None:
                break
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=input_irq_steps)
            guard += 1


def _fast_forward_wait(rt, sample, steps_left):
    """Advance the CPU through the retrace wait it is currently inside, consuming at most ``steps_left`` of
    the sub_batch's STEP budget, leaving ALL register / flag / memory state EXACTLY as the interpreted loop
    would, and returning the steps still remaining.

    The budget is counted in cpu.step() units — identical to ``play._pump_and_step``'s ``for _ in
    range(n_steps)`` — NOT in instruction_count: a hardware-interrupt entry advances ic by 0 but is still one
    step, so an ic-based budget would desync from the reference whenever IF toggles (the gameplay drift bug).

    Strategy: interpret every boundary instruction for real with ``cpu.step()`` — the entry setup, each poll
    loop's first iteration, the loop-to-loop transitions, and the exit — so all flags/registers are computed
    by the CPU itself. Only the long run of *identical* poll iterations is collapsed in closed form: arriving
    at a loop-top via its back-edge (flags == ``test(continue_bit)``), advance over the consecutive
    same-condition iterations — each is 3 instructions (in/test/jcc), so it costs 3 steps and 3 ic, with
    ip/registers/flags unchanged; only the deterministic clock (hence the sampled retrace bit) moves."""
    cpu = rt.cpu
    prev_ip = None
    while steps_left > 0:
        s = cpu.s
        if s.cs != _CS or s.ip not in _FF_NODES:
            return steps_left                       # executed the `ret` (or otherwise left the loop)
        be = _POLL_BACKEDGE.get(prev_ip)
        if be is not None and s.ip == be[0]:        # arrived at a loop-top via its back-edge -> bulk-skip
            cond = be[1]
            ic = cpu.instruction_count              # at the loop-top `in al,dx`
            k = 0
            while 3 * (k + 1) <= steps_left and sample(ic + 3 * k) == cond:
                k += 1                              # iteration k is same-condition and fits the step budget
            if k:
                cpu.instruction_count = ic + 3 * k  # skip k identical iterations (ip/regs/flags unchanged)
                steps_left -= 3 * k                 # ... costing 3 steps each, same as interpreting them
                prev_ip = s.ip                      # still at the loop-top `in`
                continue                            # re-check budget / membership before stepping
        if prev_ip == _PIT_BACKEDGE and s.ip == _PIT_TOP:   # 1C6F PIT wait: arrived at the top via jb 1c6f
            delta = _pit_wait_delta(cpu)
            if -3 < delta < 3:                      # loop condition holds; [0x27EE] is ISR-written and IRQs
                #                                     only land at segment boundaries -> it CANNOT exit inside
                #                                     this segment: skip whole identical iterations to budget.
                length = 6 if delta < 0 else 5      # mov/sub/jns/(neg)/cmp/jb — sign fixed within the segment
                k = 0
                while length * (k + 1) <= steps_left:
                    k += 1
                if k:
                    cpu.instruction_count += length * k   # ip/regs/flags identical each pass (ax + flags are
                    steps_left -= length * k              # recomputed from the same operands every iteration)
                    prev_ip = s.ip                        # still at the loop-top `mov ax,[0x27EE]`
                    continue
        prev_ip = s.ip
        cpu.step()
        steps_left -= 1
    return steps_left


def advance_frame_fast(rt, *, chunk_steps, sub_batch, clock, pic, sound_blaster, timer_irq,
                       input_irq_steps, tick_state, det_speed, active_fraction, base=0.0):
    """Fast-forward replacement for ``play._advance_demo_frame`` (same signature plus ``det_speed`` /
    ``active_fraction`` / ``base`` for the closed-form retrace sampling). Byte-equivalent to it on the
    deterministic clock; only the classified retrace polls run faster."""
    cpu = rt.cpu
    sample = make_sample(det_speed, base, active_fraction)
    remaining = chunk_steps
    while remaining > 0:
        n = min(sub_batch, remaining)
        _pump(rt, now=clock(), pic=pic, sound_blaster=sound_blaster, timer_irq=timer_irq,
              input_irq_steps=input_irq_steps, tick_state=tick_state)
        steps_left = n                              # STEP budget (cpu.step units), matching _pump_and_step
        while steps_left > 0:
            s = cpu.s
            if s.cs == _CS and s.ip in _FF_NODES:
                steps_left = _fast_forward_wait(rt, sample, steps_left)
            else:
                cpu.step()
                steps_left -= 1
        remaining -= n
