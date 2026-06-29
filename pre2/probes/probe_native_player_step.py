"""Whole-routine shadow of native_player_step (the VM-less 5850 player update) vs the ASM 5850->5A95.

At 5850 entry: seed a NativeGameState from the pre-state, run native_player_step (no VM), capture its predicted
DGROUP. At the 5A95 RET (after the ASM ran the real player update): diff predicted vs actual over DGROUP,
excluding render state (454E's [0x6CA2..0x6CA6]) + async/audio scratch (the play_sfx digital-sound state and the
PIT/SB ISR regions that land mid-routine). native_replacements=False so the ASM (incl. DC1/FSM/collision) is the
pure oracle. Replay each demo on the deterministic clock.

Run: python -m pre2.probes.probe_native_player_step
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot
from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.state import DATA_SEG, NativeGameState
from pre2.native.player import native_player_step, RENDER_OFFSETS
import play

CS = 0x1030
ENTRY = (CS, 0x5850)
RET = (CS, 0x5A95)
_BASE = (DATA_SEG << 4) & 0xFFFFF
# excluded from the player gameplay contract: 454E render slot + play_sfx digital-sound state ([0x1004-7],
# [0x282D]/[0x2874]) + PIT timer counter [0x27EE/EF] + SB DMA scratch [0x27F0-FF] + the DMA ring [0xAB0-DFF].
_EXCL = (RENDER_OFFSETS | {0x27EE, 0x27EF, 0x1004, 0x1005, 0x1006, 0x1007, 0x282D, 0x2874}
         | set(range(0x27F0, 0x2800)) | set(range(0xAB0, 0xE00)))


def _run(demo, max_frames, stats, mism):
    pb = InputDemoPlayback.load(demo)
    rt = load_pre2_snapshot(str(ROOT / "assets" / "pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; md = cpu.mem.data
    pend = {}

    def at_entry(c):
        state = NativeGameState(bytearray(md))
        try:
            native_player_step(state)
            pend["pred"] = bytes(state.data[_BASE:_BASE + 0x10000])
            stats["call"] += 1
        except Pre2HybridGap as e:
            stats[f"GAP:{str(e)[:34]}"] += 1
        interpret_current_instruction_without_hook(c)

    _SPRITE_POS = {0x4F0A, 0x4F0B, 0x4F0C, 0x4F0D}   # render-sprite screen coords (don't-care when suppressed)

    def at_ret(c):
        pred = pend.pop("pred", None)
        if pred is not None:
            post = md[_BASE:_BASE + 0x10000]
            diffs = [o for o in range(0x10000) if post[o] != pred[o] and o not in _EXCL]
            bad = [o for o in diffs if o not in _SPRITE_POS]
            sp = [o for o in diffs if o in _SPRITE_POS]
            if sp:                                    # confirm the sprite-pos diffs are all suppressed frames
                stats["sprite_pos_diff_suppressed" if post[0x4F0E] == 0xFF and post[0x4F0F] == 0xFF
                      else "SPRITE_POS_VISIBLE!"] += 1
            if bad:
                stats["BAD"] += 1
                for o in bad[:15]:
                    if len(mism) < 40:
                        mism.append(f"{Path(demo).name} [{o:#06x}] pred={pred[o]:#04x} asm={post[o]:#04x}")
            else:
                stats["ok"] += 1
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[ENTRY] = at_entry; cpu.hook_names[ENTRY] = "player_entry"
    cpu.replacement_hooks[RET] = at_ret; cpu.hook_names[RET] = "player_ret"

    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < max_frames:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        frame += 1
    print(f"  {Path(demo).name}: {frame} frames")


def main():
    demos = [("artifacts/demo_pre2_20260629_141422", 800),
             ("artifacts/demo_pre2_20260627_213332", 600),
             ("artifacts/demo_pre2_20260626_115215", 600),
             ("artifacts/demo_pre2_20260628_142652", 1500)]
    stats = Counter(); mism = []
    for d, mf in demos:
        _run(d, mf, stats, mism)
    print("\n=== native_player_step whole-routine shadow (5850 -> 5A95) ===")
    for k in sorted(stats):
        print(f"  {k:40s} {stats[k]}")
    for m in mism:
        print("  " + m)
    ok = stats["BAD"] == 0 and stats["ok"] > 0
    print("\nnative_player_step:", "PASS (DGROUP byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
