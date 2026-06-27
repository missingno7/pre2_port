"""Checkpoints for the projectile/player interaction pass (1030:88D7's two workers).

The tiny orchestrator ``88D7`` stays ASM — it runs each of the 4 thrown-weapon slots (0x4F2E) and the player
(0x4F0A) through ``8C21`` then ``899E`` and does the player bounce. We replace the two meaty workers with the
recovered :mod:`pre2.recovered.combat_interaction` functions; the VM seam + render integration live in
:mod:`pre2.bridge.object_interaction`.

Both workers share one shape: read the source sprite, run the recovered function for its write contract,
apply it to the VM, return the carry the ASM ``88D7`` branches on (``jb``/``jae``), and emulate the near RET.
They differ only in their side-effects — ``8C21`` emits a kill SFX; ``899E`` writes the level map and re-blits
the consumed tile on-screen. Verify mode predicts at entry (no mutation) and diffs the contract + carry at
each routine's two RET sites.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import object_interaction as _io
from pre2.recovered.combat_interaction import bonus_pickup_scan, projectile_vs_enemies

from .common import report
from .player import _emit_sfx

_ENEMY = (0x1030, 0x8C21)
_ENEMY_RET_HIT = (0x1030, 0x8C67)     # stc ; ret
_ENEMY_RET_MISS = (0x1030, 0x8C71)    # clc ; ret
_BONUS = (0x1030, 0x899E)
_BONUS_RET_HIT = (0x1030, 0x8A4D)     # (pop cx ; stc) ; ret
_BONUS_RET_MISS = (0x1030, 0x8A59)    # (clc ; pop cx) ; ret


def _set_cf(cpu, hit: bool) -> None:
    cpu.s.flags = (cpu.s.flags | 1) if hit else (cpu.s.flags & ~1)


@registry.replace(*_ENEMY, "projectile_vs_enemies")
def projectile_vs_enemies_hook(cpu) -> None:
    """Native replacement for the source-vs-enemy damage scan at 1030:8C21."""
    rb, rw = _io.readers(cpu.mem)
    writes, sfx, hit, _slot = projectile_vs_enemies(rb, rw, cpu.s.si & 0xFFFF)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_interaction_pending.append((writes, None, hit))
        interpret_current_instruction_without_hook(cpu)
        return

    _io.apply_ds(cpu.mem, writes)
    if sfx:
        _emit_sfx(cpu, sfx)           # play_sfx clobbers flags, so set CF after it
    _set_cf(cpu, hit)
    cpu.s.ip = cpu.pop()


@registry.replace(*_BONUS, "bonus_pickup_scan")
def bonus_pickup_scan_hook(cpu) -> None:
    """Native replacement for the source-vs-bonus pickup scan at 1030:899E."""
    rb, rw = _io.readers(cpu.mem)
    ds_writes, map_writes, redraws, hit = bonus_pickup_scan(rb, rw, cpu.s.si & 0xFFFF)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_interaction_pending.append((ds_writes, map_writes, hit))
        interpret_current_instruction_without_hook(cpu)
        return

    _io.apply_ds(cpu.mem, ds_writes)
    _io.apply_map(cpu.mem, map_writes)
    _io.redraw_tiles(cpu.mem, redraws, map_writes)   # the on-screen tile re-blit a collect triggers
    _set_cf(cpu, hit)
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Lockstep verify at each worker's two RET sites: diff every predicted DS byte + level-map byte + the
    return carry against the ASM oracle."""
    cpu.pre2_interaction_pending = []
    ds_base = (_io.DATA_SEG << 4) & 0xFFFFF

    def _diff(mem, ds_writes, map_writes):
        for off, val in ds_writes.items():
            act = mem.data[(ds_base + (off & 0xFFFF)) & 0xFFFFF]
            if act != (val & 0xFF):
                return f"ds[{off:#06x}] rec={val & 0xFF:#04x} asm={act:#04x}"
        if map_writes:
            es_base = (mem.rw(_io.DATA_SEG, _io.MAP_SEG_PTR) << 4) & 0xFFFFF
            for off, (val, width) in map_writes.items():
                act = mem.data[(es_base + (off & 0xFFFF)) & 0xFFFFF]
                if act != (val & 0xFF):
                    return f"map[{off:#06x}] rec={val & 0xFF:#04x} asm={act:#04x}"
        return None

    def _exit(name, expected_hit):
        def _hook(c) -> None:
            pending = getattr(c, "pre2_interaction_pending", None)
            if pending:
                ds_writes, map_writes, hit = pending.pop()
                if hit != expected_hit:
                    reason = f"carry rec={int(hit)} asm={int(expected_hit)}"
                else:
                    reason = _diff(c.mem, ds_writes, map_writes)
                report(stats, on_result, raise_on_divergence, name, reason)
            interpret_current_instruction_without_hook(c)
        return _hook

    for key, name, hit in (
        (_ENEMY_RET_HIT, "projectile_vs_enemies", True),
        (_ENEMY_RET_MISS, "projectile_vs_enemies", False),
        (_BONUS_RET_HIT, "bonus_pickup_scan", True),
        (_BONUS_RET_MISS, "bonus_pickup_scan", False),
    ):
        cpu.replacement_hooks[key] = _exit(name, hit)
        cpu.hook_names[key] = f"{name}_verify"
