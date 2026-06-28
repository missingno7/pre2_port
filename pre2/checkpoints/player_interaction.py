"""Checkpoint for the WHOLE player<->world interaction pass (1030:8295..8617) — the coastline collapse of the
player-vs-enemy (loop1) + player-vs-pickup (loop2) subsystem.

It is CALL'd from the main loop's flat subsystem-call list at 0x0232 (-> ret 0x0235); registers are scratch
(the caller re-derives them), so the live hook just runs the recovered :func:`player_interaction_tick` over
VM memory, emits each hit's sfx via a controlled play_sfx near-call, and does the routine's near ret. Like
object_tick / second_pass_tick this is NOT instruction-count transparent (it does the pass in one host step);
the data-segment effect is byte-exact (whole-pass shadow, pre2/probes/probe_player_interaction_tick.py).

The two ASM_MATCHED-only effect paths (trap 864F, boss-projectile 8618) are still UNWITNESSED, so under the
live hook they FAIL LOUD (strict=True -> Pre2HybridGap) rather than run an unverified recovery; the offline
shadow keeps strict=False so it verifies them the moment a demo exercises them.

VERIFY MODE keeps the ASM as oracle: predict from the entry state (no mutation), step aside (the ASM runs),
and diff the predicted game-state writes at the routine's RET.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.checkpoints.player import _emit_sfx
from pre2.recovered.object_inject import find_free_object_slot
from pre2.recovered.player_interaction import Loop2NeedsHelper, player_interaction_tick

from .common import Pre2HybridGap, report

_DS = 0x1A0F
_ENTRY = (0x1030, 0x8295)
# the routine's RET sites: loop1's early returns (stomp/hurt/death) + loop2's exits
_RETS = (0x833F, 0x8389, 0x83CD, 0x8617, 0x8858, 0x885E, 0x8829, 0x8509)


def _readers(mem):
    base = (_DS << 4) & 0xFFFFF
    rb = lambda o: mem.data[(base + (o & 0xFFFF)) & 0xFFFFF]            # noqa: E731
    rw = lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8)                  # noqa: E731
    return base, rb, rw


class _Ov:
    """Read-through overlay on VM DS memory for the verify-mode prediction (no mutation of the live VM)."""
    __slots__ = ("_rb", "w")

    def __init__(self, mem):
        _, self._rb, _ = _readers(mem)
        self.w: dict[int, int] = {}

    def rb(self, o):
        o &= 0xFFFF
        return self.w[o] if o in self.w else self._rb(o)

    def rw(self, o):
        return self.rb(o) | (self.rb((o + 1) & 0xFFFF) << 8)

    def apply(self, writes):
        for off, (val, wid) in writes.items():
            for k in range(wid):
                self.w[(off + k) & 0xFFFF] = (val >> (8 * k)) & 0xFF


@registry.replace(*_ENTRY, "player_interaction_tick")
def player_interaction_tick_hook(cpu) -> None:
    """Native replacement for the whole player<->world interaction pass (1030:8295)."""
    if getattr(cpu, "pre2_verify_mode", False):
        ov = _Ov(cpu.mem)
        read_id = lambda slot: ov.rw(0x4FD0 + slot * 0x12 + 4)          # noqa: E731
        try:
            player_interaction_tick(ov.rb, ov.rw, ov.apply, lambda s: None,
                                    lambda: find_free_object_slot(read_id), strict=False)
            cpu.pre2_pi_pending = ov.w
        except Loop2NeedsHelper:
            cpu.pre2_pi_pending = None                                  # unmapped id -> let the ASM run, no diff
        interpret_current_instruction_without_hook(cpu)
        return

    mem = cpu.mem
    base, rb, rw = _readers(mem)

    def apply_writes(writes):
        for off, (val, wid) in writes.items():
            mem.data[(base + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF
            if wid == 2:
                mem.data[(base + ((off + 1) & 0xFFFF)) & 0xFFFFF] = (val >> 8) & 0xFF

    read_id = lambda slot: rw(0x4FD0 + slot * 0x12 + 4)                 # noqa: E731
    sfx_q: list[int] = []
    try:
        player_interaction_tick(rb, rw, apply_writes, sfx_q.append,
                                lambda: find_free_object_slot(read_id), strict=True)
    except Loop2NeedsHelper as e:
        raise Pre2HybridGap(f"player_interaction_tick (1030:8295) live: ASM_MATCHED/unrecovered path hit: {e}")
    for s in sfx_q:
        _emit_sfx(cpu, [s])
    cpu.s.ip = cpu.pop()       # near ret to the main loop (0x0235); ax/bx/.. are scratch (caller re-derives)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Diff the predicted game-state writes vs the ASM at the routine's RET (live --verify-hooks coverage; the
    offline whole-pass probe is the broader byte-exact authority)."""
    cpu.pre2_pi_pending = None

    def _diff_at_ret(c) -> None:
        w = getattr(c, "pre2_pi_pending", None)
        c.pre2_pi_pending = None
        if w:
            base, _, _ = _readers(c.mem)
            reason = None
            for off, val in w.items():
                asm = c.mem.data[(base + (off & 0xFFFF)) & 0xFFFFF]
                if asm != val:
                    reason = f"[{off:#06x}] rec={val:#04x} asm={asm:#04x}"
                    break
            report(stats, on_result, raise_on_divergence, "player_interaction_tick", reason)
        interpret_current_instruction_without_hook(c)

    for ip in _RETS:
        cpu.replacement_hooks[(0x1030, ip)] = _diff_at_ret
        cpu.hook_names[(0x1030, ip)] = "player_interaction_tick_verify"
