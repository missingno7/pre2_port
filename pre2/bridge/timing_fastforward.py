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


def _fast_forward_wait(rt, sample, stop_ic):
    """Advance the CPU through the retrace wait it is currently inside, up to its ``ret`` or to ``stop_ic``
    (the sub_batch boundary), whichever comes first, leaving ALL register / flag / memory state EXACTLY as
    the interpreted loop would.

    Strategy: interpret every boundary instruction for real with ``cpu.step()`` — the entry setup
    (push/push/mov[/cmp/mov]), each poll loop's first iteration, the loop-to-loop transitions, and the exit
    (pop/pop/ret) — so all flags and registers are computed by the CPU itself. Only the long run of
    *identical* poll iterations is collapsed in closed form: when we arrive at a loop-top via its back-edge
    (so flags == ``test(continue_bit)``), advance ``instruction_count`` over the consecutive same-condition
    iterations, since each returns to the same loop-top with identical registers/flags — only the
    deterministic clock (hence the sampled retrace bit) moves."""
    cpu = rt.cpu
    prev_ip = None
    while True:
        if cpu.instruction_count >= stop_ic:
            return                                  # sub_batch boundary reached mid-spin
        s = cpu.s
        if s.cs != _CS or s.ip not in ALL_NODES:
            return                                  # executed the `ret` (or otherwise left the loop)
        be = _POLL_BACKEDGE.get(prev_ip)
        if be is not None and s.ip == be[0]:        # arrived at a loop-top via its back-edge -> bulk-skip
            cond = be[1]
            ic = cpu.instruction_count              # at the loop-top `in al,dx`
            k = 0
            while ic + 3 * (k + 1) <= stop_ic and sample(ic + 3 * k) == cond:
                k += 1                              # iteration k is same-condition and fully fits
            if k:
                cpu.instruction_count = ic + 3 * k  # skip k identical iterations (ip/regs/flags unchanged)
                prev_ip = s.ip                      # still at the loop-top `in`
                continue                            # re-check stop_ic / membership before stepping
        prev_ip = s.ip
        cpu.step()


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
        stop_ic = cpu.instruction_count + n
        while cpu.instruction_count < stop_ic:
            s = cpu.s
            if s.cs == _CS and s.ip in ALL_NODES:
                _fast_forward_wait(rt, sample, stop_ic)
            else:
                cpu.step()
        remaining -= n
