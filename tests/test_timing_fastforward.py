"""Snapshot-free proof that the experimental retrace fast-forward equals naive instruction-stepping.

``pre2.bridge.timing_fastforward._fast_forward_wait`` collapses long runs of identical retrace-poll
iterations into a single ``instruction_count`` jump. This test drives a tiny mock CPU that interprets the
exact classified loop CFG (``vga_timing.ALL_NODES``: in / test / je-jne / ret) and asserts that, for a sweep
of clock phases and stop budgets, the fast-forward leaves the CPU at the IDENTICAL (instruction_count, ip)
as stepping the same mock one instruction at a time. (The CFG-vs-real-ASM fidelity is proven separately and
byte-exact by the snapshot probes pre2/probes/verify_vga_timing.py and verify_fast_retrace.py.)

This guards the bulk-skip arithmetic + the off-by-one boundary handling against regressions with no large
fixtures: the reference and the fast path share the same `step()`, so any divergence is the skip logic.
"""
from __future__ import annotations

from types import SimpleNamespace

from pre2.bridge.timing_fastforward import _CS, _fast_forward_wait, make_sample
from pre2.recovered.vga_timing import ALL_NODES

_ENTRIES = (0x9900, 0x990D, 0x44CD)


class _MockCPU:
    """Interprets the ALL_NODES retrace-loop CFG: `in` samples the bit, `br` branches on it, `ret` leaves
    the loop region (ip -> a non-node sentinel). One instruction per step(), +1 instruction_count."""

    def __init__(self, ip, sample, ic=0):
        self.s = SimpleNamespace(cs=_CS, ip=ip, ax=0x0000, dx=0x0000)
        self.instruction_count = ic
        self._sample = sample
        self._bit = False

    def step(self):
        node = ALL_NODES[self.s.ip]
        kind = node[0]
        if kind == "in":
            self._bit = self._sample(self.instruction_count)
            self.s.ax = (self.s.ax & 0xFF00) | (0x08 if self._bit else 0x00)
            self.s.dx = 0x03DA
            nxt = node[1]
        elif kind == "op":
            nxt = node[1]
        elif kind == "br":
            nxt = node[1] if self._bit else node[2]
        else:                                   # "ret": leave the loop region
            nxt = 0x0001                        # sentinel ip, not in ALL_NODES
        self.instruction_count += 1
        self.s.ip = nxt


def _ref_advance(entry, sample, ic0, stop_ic):
    """Naive reference: step the mock one instruction at a time until stop_ic or it leaves the loop."""
    cpu = _MockCPU(entry, sample, ic0)
    while cpu.instruction_count < stop_ic and cpu.s.cs == _CS and cpu.s.ip in ALL_NODES:
        cpu.step()
    return cpu.instruction_count, cpu.s.ip


def _fast_advance(entry, sample, ic0, stop_ic):
    cpu = _MockCPU(entry, sample, ic0)
    rt = SimpleNamespace(cpu=cpu)
    _fast_forward_wait(rt, sample, stop_ic)
    return cpu.instruction_count, cpu.s.ip


def test_fast_forward_equals_naive_stepping_all_phases():
    det_speed = 6428 * 70          # headless det clock scale (chunk_steps * present_hz)
    af = 0.06
    sample = make_sample(det_speed, 0.0, af)
    checked = 0
    for entry in _ENTRIES:
        # sweep ic0 across a couple of full refresh periods so every retrace phase (and the SET pulse) occurs
        for ic0 in range(0, det_speed // 35 + 1, 7):     # ~2 refresh periods, fine step
            for budget in (3, 4, 6, 9, 30, 300, 3000, 30000, 200000):
                stop_ic = ic0 + budget
                assert _fast_advance(entry, sample, ic0, stop_ic) == \
                    _ref_advance(entry, sample, ic0, stop_ic), \
                    f"divergence entry={entry:#06x} ic0={ic0} budget={budget}"
                checked += 1
    assert checked > 5000


def test_fast_forward_reaches_ret_within_a_full_frame():
    # With a whole-frame budget the wait must run to completion (leave the loop) for every entry/phase.
    det_speed = 6428 * 70
    sample = make_sample(det_speed, 0.0, 0.06)
    for entry in _ENTRIES:
        for ic0 in range(0, det_speed, 137):
            ic, ip = _fast_advance(entry, sample, ic0, ic0 + det_speed)   # 1s of emulated time
            assert ip not in ALL_NODES, f"entry={entry:#06x} ic0={ic0} did not exit (ip={ip:#06x})"
