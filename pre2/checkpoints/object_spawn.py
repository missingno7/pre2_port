"""Checkpoints for the 6822 spawner island — the two recovered per-frame gameplay branches it dispatches.

6822 (run once per frame before the object walker 684E) gates three event routines:

  * 70D7  call  when [0x91FE]!=0xFF   -> the level camera/scroll engine  (camera_engine)
  * 6D34  call  when [0x2D8A]==5 ...   -> a mode-5 event (unrecovered, left as ASM; unwitnessed)
  * 6ADD  call  when [0x2D8A]==9       -> the mode-9 last-boss engine      (tick_mode9_boss)

then falls through to 684E (object_tick, hooked separately). Both branches are near ``call``s, so each hook
runs the recovered function over live VM memory, writes the contract back, and does a near ret. Like the
object_tick collapse these are NOT instruction-count-transparent (one host step vs thousands) -> demos must be
re-recorded; the data-segment effect is whole-DGROUP byte-exact (camera_engine 281 calls / tick_mode9_boss 659
calls, 0 div; see pre2/probes shadows). The boss-death finale (6C0D/6BDB), the camera state-6 finale (94F3),
and any unrecovered path fail loud (Pre2HybridGap) — never a silent ASM fallback.

VERIFY MODE: each hook steps aside (runs the interpreted ASM) so the lockstep oracle exercises the original;
the composed passes are verified offline by the whole-DGROUP shadows.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge.object_spawn import apply_ds, readers, tile_reader
from pre2.checkpoints.common import Pre2HybridGap
from pre2.recovered.object_spawn import Pre2SpawnGap, camera_engine, tick_mode9_boss


@registry.replace(0x1030, 0x70D7, "camera_engine")
def camera_engine_hook(cpu) -> None:
    """Native replacement for the 70D7 camera/scroll engine (1030:70D7..7579)."""
    if getattr(cpu, "pre2_verify_mode", False):
        interpret_current_instruction_without_hook(cpu)
        return
    rb, rw = readers(cpu.mem)
    try:
        writes = camera_engine(rb, rw, tile_reader(cpu.mem))
    except Pre2SpawnGap as exc:                       # the state-6 boss-reach finale (94F3) is unrecovered
        raise Pre2HybridGap(f"70D7 camera_engine: {exc}") from exc
    apply_ds(cpu.mem, writes)
    cpu.s.ip = cpu.pop()                              # near ret to 6822 (682C)


@registry.replace(0x1030, 0x6ADD, "tick_mode9_boss")
def tick_mode9_boss_hook(cpu) -> None:
    """Native replacement for the 6ADD mode-9 last-boss engine (1030:6ADD..6BDA)."""
    if getattr(cpu, "pre2_verify_mode", False):
        interpret_current_instruction_without_hook(cpu)
        return
    rb, rw = readers(cpu.mem)
    try:
        writes = tick_mode9_boss(rb, rw)
    except Pre2SpawnGap as exc:                       # boss-death finale (6C0D) / death-burst (6BDB) unrecovered
        raise Pre2HybridGap(f"6ADD tick_mode9_boss: {exc}") from exc
    apply_ds(cpu.mem, writes)
    cpu.s.ip = cpu.pop()                              # near ret to 6822 (684E)
