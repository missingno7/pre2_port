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


def _ref_advance(entry, sample, ic0, budget):
    """Naive reference: step the mock one instruction at a time, consuming a STEP budget (one step per
    cpu.step(), matching play._pump_and_step's `for _ in range(n_steps)`) or until it leaves the loop."""
    cpu = _MockCPU(entry, sample, ic0)
    steps = budget
    while steps > 0 and cpu.s.cs == _CS and cpu.s.ip in ALL_NODES:
        cpu.step()
        steps -= 1
    return cpu.instruction_count, cpu.s.ip, steps


def _fast_advance(entry, sample, ic0, budget):
    cpu = _MockCPU(entry, sample, ic0)
    rt = SimpleNamespace(cpu=cpu)
    left = _fast_forward_wait(rt, sample, budget)
    return cpu.instruction_count, cpu.s.ip, left


def test_fast_forward_equals_naive_stepping_all_phases():
    det_speed = 6428 * 70          # headless det clock scale (chunk_steps * present_hz)
    af = 0.06
    sample = make_sample(det_speed, 0.0, af)
    checked = 0
    for entry in _ENTRIES:
        # sweep ic0 across a couple of full refresh periods so every retrace phase (and the SET pulse) occurs
        for ic0 in range(0, det_speed // 35 + 1, 7):     # ~2 refresh periods, fine step
            for budget in (3, 4, 6, 9, 30, 300, 3000, 30000, 200000):
                # identical final (instruction_count, ip) AND identical leftover step budget
                assert _fast_advance(entry, sample, ic0, budget) == \
                    _ref_advance(entry, sample, ic0, budget), \
                    f"divergence entry={entry:#06x} ic0={ic0} budget={budget}"
                checked += 1
    assert checked > 5000


def test_fast_forward_reaches_ret_within_a_full_frame():
    # With a whole-frame budget the wait must run to completion (leave the loop) for every entry/phase.
    det_speed = 6428 * 70
    sample = make_sample(det_speed, 0.0, 0.06)
    for entry in _ENTRIES:
        for ic0 in range(0, det_speed, 137):
            ic, ip, _ = _fast_advance(entry, sample, ic0, det_speed)   # 1s of emulated time
            assert ip not in ALL_NODES, f"entry={entry:#06x} ic0={ic0} did not exit (ip={ip:#06x})"


# --- The 1C6F PIT frame-limiter wait (the ~23Hz gate: spin until |[0x27EE] - cs:[0x1D67]| >= 3) -----------
# Same proof shape as the retrace loops: a mock interpreting the exact 1C6F CFG (mov/sub/jns/[neg]/cmp/jb,
# 5 instructions per iteration, 6 when the delta is negative) must land at the IDENTICAL (instruction_count,
# ip, leftover budget) as the bulk-skip. The condition memory ([0x27EE]) is ISR-written and IRQs land only at
# segment boundaries, so within one _fast_forward_wait call the delta is constant — exactly what the mock
# models. (Whole-VM byte-equivalence — 0 memory diff, identical ic, 200-300 frames, pure + safe modes — is
# proven against the real interpreter by the same-session probe runs and the verify_fast_retrace probe.)

_PIT_CFG = {
    0x1C6F: ("mov", 0x1C72),          # mov ax,[0x27EE]
    0x1C72: ("sub", 0x1C77),          # sub ax,cs:[0x1D67]
    0x1C77: ("jns", 0x1C7B, 0x1C79),  # jns 1c7b (taken when delta >= 0)
    0x1C79: ("neg", 0x1C7B),          # neg ax
    0x1C7B: ("cmp", 0x1C7E),          # cmp ax,3
    0x1C7E: ("jb", 0x1C6F, 0x1C80),   # jb 1c6f (taken while |delta| < 3); 1c80 = outside the node set
}


class _MockPitCPU:
    """Interprets the 1C6F CFG over a tiny memory image (the two condition words)."""

    def __init__(self, ip, delta, ic=0):
        self.s = SimpleNamespace(cs=_CS, ip=ip, ax=0)
        self.instruction_count = ic
        data = bytearray(0x110000)
        ref = 0x4000
        cur = (ref + delta) & 0xFFFF
        data[(0x1A0F << 4) + 0x27EE:(0x1A0F << 4) + 0x27F0] = cur.to_bytes(2, "little")
        data[(_CS << 4) + 0x1D67:(_CS << 4) + 0x1D69] = ref.to_bytes(2, "little")
        self.mem = SimpleNamespace(data=data)

    def step(self):
        node = _PIT_CFG[self.s.ip]
        kind = node[0]
        d = self.mem.data
        if kind == "mov":
            self.s.ax = d[(0x1A0F << 4) + 0x27EE] | (d[(0x1A0F << 4) + 0x27EF] << 8)
            nxt = node[1]
        elif kind == "sub":
            ref = d[(_CS << 4) + 0x1D67] | (d[(_CS << 4) + 0x1D68] << 8)
            self.s.ax = (self.s.ax - ref) & 0xFFFF
            nxt = node[1]
        elif kind == "jns":
            nxt = node[2] if self.s.ax & 0x8000 else node[1]
        elif kind == "neg":
            self.s.ax = (-self.s.ax) & 0xFFFF
            nxt = node[1]
        elif kind == "cmp":
            nxt = node[1]
        else:                                     # jb: loop while ax (=|delta|) < 3
            nxt = node[1] if self.s.ax < 3 else node[2]
        self.instruction_count += 1
        self.s.ip = nxt


def _pit_ref(delta, ic0, budget):
    from pre2.bridge.timing_fastforward import _FF_NODES
    cpu = _MockPitCPU(0x1C6F, delta, ic0)
    steps = budget
    while steps > 0 and cpu.s.cs == _CS and cpu.s.ip in _FF_NODES:
        cpu.step()
        steps -= 1
    return cpu.instruction_count, cpu.s.ip, steps, cpu.s.ax


def _pit_fast(delta, ic0, budget):
    cpu = _MockPitCPU(0x1C6F, delta, ic0)
    rt = SimpleNamespace(cpu=cpu)
    sample = make_sample(6428 * 70, 0.0, 0.06)     # retrace sampler; unused by the PIT branch
    left = _fast_forward_wait(rt, sample, budget)
    return cpu.instruction_count, cpu.s.ip, left, cpu.s.ax


def test_pit_wait_fast_forward_equals_naive_stepping():
    checked = 0
    for delta in (-2, -1, 0, 1, 2):                # loop-forever deltas (constant within a segment)
        for budget in (1, 2, 4, 5, 6, 7, 11, 12, 29, 300, 2000, 30000):
            for ic0 in (0, 3, 1234):
                assert _pit_fast(delta, ic0, budget) == _pit_ref(delta, ic0, budget), \
                    f"divergence delta={delta} budget={budget} ic0={ic0}"
                checked += 1
    for delta in (3, -3, 7, 100, -32768):          # exit-immediately deltas: no skip, plain interpretation
        for budget in (1, 5, 6, 20):
            assert _pit_fast(delta, 0, budget) == _pit_ref(delta, 0, budget), \
                f"divergence delta={delta} budget={budget}"
            checked += 1
    assert checked > 190
