"""Checkpoint for the input-decode island — 1030:0DC1 decode_input.

DC1 is the front of the per-frame player update (called from the 5850 wrapper at 0x58A4): it decodes the input
source ([0x2879]: live keyboard / demo playback / record) into the six FSM input flags [0x27E8..0x27ED] and, on
the live/record path, RLE-appends the packed input to the demo buffer. It's a near ``call``, so the hook runs
the recovered decoder over live VM memory, writes the contract back, and does a near ret.

Byte-exact vs the ASM whole-routine over the demos (pre2/probes/probe_input_decode_shadow.py: 144 calls, 0 div).
The joystick-present branch (a port 0x201 hardware read) fails loud (Pre2HybridGap) — keyboard/demo play never
reaches it. VERIFY MODE: the hook steps aside so the lockstep oracle exercises the original ASM.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.views.input_decode import apply_ds, readers
from pre2.gaps import Pre2HybridGap
from pre2.recovered.input_decode import decode_input, Pre2InputGap


@registry.replace(0x1030, 0x0DC1, "input_decode")
def input_decode_hook(cpu) -> None:
    """Native replacement for DC1 (1030:0DC1..0F7E)."""
    if getattr(cpu, "pre2_verify_mode", False):
        interpret_current_instruction_without_hook(cpu)
        return
    rb, rw = readers(cpu.mem)
    try:
        writes = decode_input(rb, rw)
    except Pre2InputGap as exc:                       # joystick port 0x201 read — keyboard/demo play never hits
        raise Pre2HybridGap(f"0DC1 decode_input: {exc}") from exc
    apply_ds(cpu.mem, writes)
    cpu.s.ip = cpu.pop()                              # near ret into the 5850 player wrapper (58A7)
