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
from pre2.gaps import Pre2HybridGap
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.recovered.object_spawn import (BOSS_SONG_INDEX, GLYPH_LATCH, Pre2SpawnGap, SONG_REQUEST,
                                          camera_engine, tick_mode9_boss)
from pre2.recovered.render_frame import draw_boss_glyph


def _blit_boss_glyph(mem, glyph: int) -> None:
    """[asm 6C0D] Perform the boss-glyph blit the replaced ASM would have done: the 6x7 level-tile window
    ([glyph*6+0x14], tilemap row stride 0x100) latched into the staging page of the VM's EGA plane shadows."""
    d = mem.data
    base = 0x1A0F << 4
    es = (d[base + 0x2DDA] | (d[base + 0x2DDB] << 8)) << 4
    tbl = glyph * 6 + 0x14
    tiles = bytearray(42)
    for row in range(7):
        src = es + tbl + row * 0x100
        tiles[row * 6:row * 6 + 6] = d[src:src + 6]
    planes = [memoryview(d)[EGA_APERTURE + p * EGA_PLANE_STRIDE:EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]
              for p in range(4)]
    draw_boss_glyph(planes, bytes(tiles))


def _ret_or_song(cpu, writes) -> None:
    """Finish a spawn-engine hook: pop the SONG_REQUEST sentinel ([asm 7585: mov ax,0xD; call 02CC] — the boss
    music). When set, TRANSFER to the real 02CC with ax=idx instead of returning: the loader runs as ASM (module
    into memory -> the SB tracker + the enhanced-audio 02CC observers both hear it) and its RET pops the
    caller's return address — exactly the ASM's tail-call shape. Without this the hook swallowed the boss-music
    switch (user-visible: the music never changed at the boss)."""
    song = writes.pop(SONG_REQUEST, None)
    glyph = writes.pop(GLYPH_LATCH, None)                 # [asm 6BD3] the boss glyph this tick
    if glyph is not None:
        cpu.mem.pre2_boss_glyph = glyph                   # for the faithful renderer's state capture
        _blit_boss_glyph(cpu.mem, glyph)                  # [asm 6BD7 call 6C0D] the hook replaced the ASM that
        #   performed the blit — without this the boss image FREEZES at its old pose in the hybrid (user-visible:
        #   the boss never animates or reacts to hits)
    apply_ds(cpu.mem, writes)
    if song is not None:
        cpu.s.ax = song & 0xFFFF                          # [asm 7599] mov ax,0xd
        cpu.s.ip = 0x02CC                                 # run the real song loader; its ret exits to the caller
    else:
        cpu.s.ip = cpu.pop()                              # near ret to 6822


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
    _ret_or_song(cpu, writes)                         # ret to 6822 (682C), via the real 02CC on the boss switch


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
    _ret_or_song(cpu, writes)                         # ret to 6822 (684E), via the real 02CC on the boss switch
