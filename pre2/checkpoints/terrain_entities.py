"""Checkpoint for the terrain/moving-entity system (1030:4907) — see :mod:`pre2.recovered.terrain_entities`.

CALL'd from the main-loop spine (1030:022C) with scratch registers, so the live hook runs the recovered
whole-routine transform over VM memory, applies its byte-level DGROUP contract, and does the near ret. Like
player_interaction this collapses the whole routine (movement + render projection + the 4B05/64DF/8D7B
player-ride collision) into one host step; the data-segment effect is byte-exact (whole-DGROUP shadow, 0
divergences over 1116 calls across the gorilla / 233821 / 181725 demos, excluding the audio-ISR scratch).

Verify mode keeps the ASM as oracle: predict the contract from the entry state (no mutation), step aside, and
diff it at the routine's RET (0x4B04).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.views import effects_update as bridge
from pre2.recovered.terrain_entities import tick_terrain_entities

from pre2.gaps import report

_DS_BASE = (0x1A0F << 4) & 0xFFFFF
_RET = 0x4B04


@registry.replace(0x1030, 0x4907, "tick_terrain_entities")
def _terrain_entities_hook(cpu) -> None:
    """Native replacement for the whole terrain-entity tick (1030:4907)."""
    mem = cpu.mem
    rb, rw = bridge.readers(mem)
    writes = tick_terrain_entities(rw, rb, bridge.tile_reader(mem))
    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_te_pending = writes
        interpret_current_instruction_without_hook(cpu)
        return
    for off, val in writes.items():
        mem.data[(_DS_BASE + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF
    cpu.s.ip = cpu.pop()       # near ret to the main-loop spine; regs are scratch


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Diff the predicted byte-level DGROUP contract vs the ASM at the routine's RET (live --verify-hooks)."""
    cpu.pre2_te_pending = None

    def _diff(c) -> None:
        writes = getattr(c, "pre2_te_pending", None)
        c.pre2_te_pending = None
        if writes:
            reason = None
            for off, val in writes.items():
                asm = c.mem.data[(_DS_BASE + (off & 0xFFFF)) & 0xFFFFF]
                if asm != (val & 0xFF):
                    reason = f"[{off:#06x}] rec={val & 0xFF:#04x} asm={asm:#04x}"
                    break
            report(stats, on_result, raise_on_divergence, "tick_terrain_entities", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(0x1030, _RET)] = _diff
    cpu.hook_names[(0x1030, _RET)] = "tick_terrain_entities_verify"
