"""Checkpoint for the projectile/player interaction pass (1030:88D7's two workers).

The tiny orchestrator ``88D7`` stays ASM — it just runs each of the 4 thrown-weapon slots (0x4F2E) and the
player (0x4F0A) through ``8C21`` then ``899E`` and does the player bounce. We replace the two meaty workers
with the recovered :mod:`pre2.recovered.combat_interaction` functions:

* ``8C21`` projectile/player-vs-ENEMY damage — pure DS state + a kill SFX, returns CF=hit. Live-hooked here.
* ``899E`` projectile/player-vs-BONUS pickup — DS + level-map state + an on-screen tile re-blit. (next)

Each worker reads live VM state via the bridge readers, runs the recovered function for its
``{offset: value}`` write contract, applies it, returns the carry the ASM ``88D7`` branches on, and emulates
the near RET. In verify mode the original ASM is the oracle: predict at entry (no mutation), diff the byte
contract + the return carry at the routine's RET sites.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.combat_interaction import projectile_vs_enemies

from .common import report
from .player import _emit_sfx

_DS = 0x1A0F
_ENEMY_ENTRY = (0x1030, 0x8C21)
_ENEMY_EXIT_HIT = (0x1030, 0x8C67)    # stc ; ret  (an enemy was hit)
_ENEMY_EXIT_MISS = (0x1030, 0x8C71)   # clc ; ret  (no enemy in range)


def _readers(mem):
    ds_base = (_DS << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(ds_base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        b = (ds_base + (o & 0xFFFF)) & 0xFFFFF
        return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)

    return rb, rw, ds_base


def _set_cf(cpu, hit: bool) -> None:
    cpu.s.flags = (cpu.s.flags | 1) if hit else (cpu.s.flags & ~1)


@registry.replace(*_ENEMY_ENTRY, "projectile_vs_enemies")
def projectile_vs_enemies_hook(cpu) -> None:
    """Native replacement for the source-vs-enemy damage scan at 1030:8C21."""
    mem = cpu.mem
    rb, rw, ds_base = _readers(mem)
    writes, sfx, hit, _slot = projectile_vs_enemies(rb, rw, cpu.s.si & 0xFFFF)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_interaction_pending.append((writes, hit))
        interpret_current_instruction_without_hook(cpu)
        return

    for off, val in writes.items():
        mem.data[(ds_base + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF
    if sfx:
        _emit_sfx(cpu, sfx)           # play_sfx clobbers flags, so set CF after it
    _set_cf(cpu, hit)
    cpu.s.ip = cpu.pop()             # near ret (the carry the ASM 88D7 reads via jb)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Lockstep verify at 8C21's two RET sites: diff every predicted DS byte + the return carry."""
    cpu.pre2_interaction_pending = []

    def _verify_exit(expected_hit):
        def _hook(c) -> None:
            pending = getattr(c, "pre2_interaction_pending", None)
            if pending:
                writes, hit = pending.pop()
                ds_base = (_DS << 4) & 0xFFFFF
                reason = None
                if hit != expected_hit:
                    reason = f"carry rec={int(hit)} asm={int(expected_hit)}"
                else:
                    for off, val in writes.items():
                        act = c.mem.data[(ds_base + (off & 0xFFFF)) & 0xFFFFF]
                        if act != (val & 0xFF):
                            reason = f"ds[{off:#06x}] rec={val & 0xFF:#04x} asm={act:#04x}"
                            break
                report(stats, on_result, raise_on_divergence, "projectile_vs_enemies", reason)
            interpret_current_instruction_without_hook(c)
        return _hook

    cpu.replacement_hooks[_ENEMY_EXIT_HIT] = _verify_exit(True)
    cpu.replacement_hooks[_ENEMY_EXIT_MISS] = _verify_exit(False)
    cpu.hook_names[_ENEMY_EXIT_HIT] = "projectile_vs_enemies_verify"
    cpu.hook_names[_ENEMY_EXIT_MISS] = "projectile_vs_enemies_verify"
