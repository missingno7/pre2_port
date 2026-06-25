"""Recovered VGA retrace busy-wait primitives — deterministic closed-form (pure).

Stage C of the timing-hook pass (see docs/pre2/timing_hook_design.md). These are faithful
instruction-counting walks of the three classified retrace wait loops (1030:9900 / 990D / 44CD). Each walk
mirrors the loop's exact control flow, counting one ``instruction_count`` per executed instruction and
sampling the retrace bit at each ``in al,dx`` via an injected ``sample(ic)`` callback — so it predicts, for a
**poll-only segment** (no mid-spin ISR), the exact ``instruction_count`` at which the interpreted ASM loop
would ``ret``, the final retrace bit, and the iteration count.

This is the closed form ONLY; it does not advance any CPU, deliver IRQs, or touch memory. The fast-forward
hook (a later stage) drives the CPU between real ISR deliveries and uses this to skip the interpreted polls.
A mid-spin ISR is NOT modelled here (it runs for real between segments) — see the design note §5/§5b.

``sample(ic)`` must return the retrace bit state (True = SET) the VM's ``_vga_status`` would return at
``instruction_count == ic`` (a pure function of the deterministic clock). Every ``in al,dx`` also resets the
attribute-controller flip-flop to index mode; the net effect after any number of reads is ``False``, which
the walks report via ``attr_flipflop_reset=True`` (it is reset iff at least one ``in`` executed — always, for
these loops).

Pure: no ``cpu``/``mem``/``dos_re`` imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

__all__ = ["WaitResult", "simulate_retrace_start", "simulate_retrace_edge",
           "simulate_present_edge", "SIMULATORS"]


@dataclass(frozen=True)
class WaitResult:
    """Closed-form prediction of one poll-only retrace-wait segment.

    ``instrs`` = the number of instructions the loop executes from entry to (and including) the ``ret`` →
    the exit ``instruction_count`` is ``ic0 + instrs``. ``iterations`` = the number of ``in al,dx`` samples
    taken. ``final_bit`` = the retrace bit at the exiting sample (always True — all three loops exit on SET).
    ``attr_flipflop_reset`` = whether the attribute-controller flip-flop ends reset (True iff any ``in`` ran).
    """
    instrs: int
    iterations: int
    final_bit: bool
    attr_flipflop_reset: bool


def simulate_retrace_start(ic0: int, sample: Callable[[int], bool]) -> WaitResult:
    """1030:9900 ``wait_for_retrace_start`` — poll 0x3DA bit3 until SET.

    9900 push ax / 9901 push dx / 9902 mov dx,0x3DA  (3) ; loop 9905 in/9906 test/9908 je (3, je taken while
    CLEAR) ; exit iter ends not-taken then 990A pop dx/990B pop ax/990C ret (3).
    """
    n = 3                                   # push ax, push dx, mov dx
    it = 0
    while True:
        s = sample(ic0 + n); it += 1        # 9905 in al,dx
        n += 3                              # in, test, je
        if s:                              # 9908 je not taken (bit SET) -> exit
            break
    n += 3                                  # pop dx, pop ax, ret
    return WaitResult(n, it, True, True)


def simulate_retrace_edge(ic0: int, sample: Callable[[int], bool]) -> WaitResult:
    """1030:990D ``wait_for_retrace_edge`` — wait while SET (falling), re-read, then (if CLEAR) wait until SET.

    990D push/push/mov (3) ; 9912 in/9913 test/9915 jne (loop while SET) ; 9917 in/9918 test/991A je 9905
    (if CLEAR fall into the 9905 loop = wait until SET, exit via 990A; if SET exit via 991C). All paths
    restore ax/dx and ret (3).
    """
    n = 3
    it = 0
    while True:                             # loopA: wait while SET
        s = sample(ic0 + n); it += 1        # 9912 in
        n += 3                              # in, test, jne
        if not s:                          # 9915 jne not taken (CLEAR) -> falling edge reached
            break
    s = sample(ic0 + n); it += 1            # 9917 in
    n += 3                                  # in, test, je
    if s:                                  # 991A je not taken (SET) -> 991C pop/pop/ret
        n += 3
        return WaitResult(n, it, True, True)
    while True:                             # je taken (CLEAR) -> 9905 loop: wait while CLEAR until SET
        s = sample(ic0 + n); it += 1        # 9905 in
        n += 3                              # in, test, je
        if s:                              # 9908 je not taken (SET) -> exit
            break
    n += 3                                  # 990A pop/pop/ret
    return WaitResult(n, it, True, True)


def simulate_present_edge(ic0: int, sample: Callable[[int], bool]) -> WaitResult:
    """1030:44CD ``wait_for_retrace_edge`` (COLOR 0x3DA path; cs:[1]!=0 on the GOG build).

    44CD push/push (2) / 44CF cmp cs:[1],0 / 44D5 je (not taken) / 44D7 mov dx,0x3DA / 44DA mov ah,8 (6
    setup) ; loopA 44DC in/44DD test/44DF jne (while SET) ; loopB 44E1 in/44E2 test/44E4 je (while CLEAR) ;
    44E6 pop dx/44E7 pop ax/44E8 ret (3).
    """
    n = 6                                   # push,push,cmp,je(not taken),mov dx,mov ah
    it = 0
    while True:                             # loopA: wait while SET
        s = sample(ic0 + n); it += 1        # 44DC in
        n += 3                              # in, test, jne
        if not s:
            break
    while True:                             # loopB: wait while CLEAR
        s = sample(ic0 + n); it += 1        # 44E1 in
        n += 3                              # in, test, je
        if s:
            break
    n += 3                                  # pop dx, pop ax, ret
    return WaitResult(n, it, True, True)


# entry IP -> (name, simulator)
SIMULATORS = {
    0x9900: ("wait_for_retrace_start", simulate_retrace_start),
    0x990D: ("wait_for_retrace_edge", simulate_retrace_edge),
    0x44CD: ("wait_for_present_edge", simulate_present_edge),
}
