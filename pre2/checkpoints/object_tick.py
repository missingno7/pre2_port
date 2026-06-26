"""Checkpoint for the COMPOSED object-update walker (1030:684E..6913) — the coastline collapse.

In production (live / replay) this single hook replaces the entire per-slot walker with the recovered
:func:`~pre2.recovered.object_tick.object_tick`: it runs the whole pass (apply_velocity -> terrain_collision
-> advance_animation -> AI dispatch -> effect spawns) over the live VM memory in place, then resumes at 0x6913
(the walker is INLINE — it falls through into the secondary-list pass, not a CALL/RET). This subsumes the
per-leaf object_velocity hook and runs the object simulation natively.

Because the recovered pass does the work in one host step instead of the thousands the ASM would, it is NOT
instruction-count-transparent — it deliberately changes the per-frame instruction count, so demos recorded
against the interpreted walker must be re-recorded. The data-segment effect is byte-exact: object_tick
reproduces every byte the ASM walker writes (whole-0x1A0F-segment lockstep = 0 diff over L6/earthquake/L7;
see pre2/probes/probe_object_tick_composed.py).

VERIFY MODE: this hook steps aside (runs the interpreted ASM) so the lockstep oracle still exercises the
per-leaf hooks (object_velocity etc.); the composed pass is verified offline by the whole-tick probe.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge.object_tick import LiveWalkerMem
from pre2.recovered.object_tick import object_tick

_ENTRY = (0x1030, 0x684E)     # mov si,0x4FD0  (walker entry)
_EXIT_IP = 0x6913             # the instruction after the walker loop (falls through to the 2nd-list pass)
_LIST_END_SI = 0x50A8         # 0x4FD0 + 12 * 0x12 — si after the loop walks all 12 slots


@registry.replace(*_ENTRY, "object_tick")
def object_tick_hook(cpu) -> None:
    """Native replacement for the whole object-update walker at 1030:684E..6913."""
    if getattr(cpu, "pre2_verify_mode", False):
        # Step aside under the oracle: let the interpreted walker run so the per-leaf verify hooks fire.
        interpret_current_instruction_without_hook(cpu)
        return

    object_tick(LiveWalkerMem(cpu))

    # Resume at the walker exit with the registers the ASM loop leaves: si past the 12-slot list, bp counted
    # down to 0, cl = 4 (the shift constant; ch is preserved across the loop). The 2nd-list pass re-derives
    # ax/bx/dx from si and manages its own es/di, so the walker's per-slot register leftovers are dead here.
    s = cpu.s
    s.si = _LIST_END_SI
    s.bp = 0
    s.cx = (s.cx & 0xFF00) | 0x04
    s.ip = _EXIT_IP
