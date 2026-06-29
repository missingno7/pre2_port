"""Whole-routine shadow of decode_input (1030:0DC1) vs the ASM's 0DC1->0F7E.

Hook DC1 entry: snapshot DGROUP + predict the recovered write contract. Hook the single RET (0F7E): assert every
predicted write matches the ASM-mutated memory, and that no byte the ASM changed was left unmodeled (excluding
async ISR scratch — the PIT timer tick + SB DMA ring can land mid-routine). DC1 is never live-hooked here, so its
ASM is the oracle. Replay every demo on the deterministic clock (mode 0 / live, as recorded).

Run: python -m pre2.probes.probe_input_decode_shadow
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
from pre2.recovered.input_decode import decode_input, Pre2InputGap
import play

CS = 0x1030
ENTRY = (CS, 0x0DC1)
RET = (CS, 0x0F7E)
# async ISR writes that can land between DC1 entry and RET (NOT DC1's contract): the INT 08 timer counter
# [0x27EE/EF] + digital-sound state [0x1004-7]/[0x27F0-FF] + the SB DMA ring buffer [0xAB0-0xDFF].
_ISR = frozenset({0x27EE, 0x27EF} | set(range(0x27F0, 0x2800)) |
                 {0x1004, 0x1005, 0x1006, 0x1007} | set(range(0xAB0, 0xE00)))


def _run(demo, max_frames, stats, mism):
    pb = InputDemoPlayback.load(demo)
    rt = load_pre2_snapshot(str(ROOT / "assets" / "pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; md = cpu.mem.data
    pend = {}

    def base():
        return (cpu.s.ds << 4) & 0xFFFFF

    def rb(o):
        return md[(base() + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        return rb(o) | (rb((o + 1) & 0xFFFF) << 8)

    def at_entry(c):
        snap = bytes(md[base():base() + 0x10000])
        try:
            pend["w"] = (decode_input(rb, rw), snap)
            stats["call"] += 1
        except Pre2InputGap as e:
            stats[f"GAP:{e}"[:40]] += 1
        interpret_current_instruction_without_hook(c)

    def at_ret(c):
        d = pend.pop("w", None)
        if d is not None:
            w, snap = d
            post = md[base():base() + 0x10000]
            bad = next((f"PRED [{o:#06x}]={v:#04x} asm={post[o]:#04x}"
                        for o, (v, wid) in w.items()
                        for k in range(wid)
                        if post[(o + k) & 0xFFFF] != ((v >> (8 * k)) & 0xFF)), None)
            if bad:
                stats["BAD"] += 1
                if len(mism) < 30:
                    mism.append(f"{Path(demo).name}: {bad}")
            else:
                if w:
                    stats["hit"] += 1
                predicted = {(o + k) & 0xFFFF for o, (v, wid) in w.items() for k in range(wid)}
                un = [o for o in range(0x10000)
                      if post[o] != snap[o] and o not in predicted and o not in _ISR]
                if un:
                    stats["UNMODELED"] += len(un)
                    for o in un[:12]:
                        if len(mism) < 40:
                            mism.append(f"{Path(demo).name}: UNMODELED [{o:#06x}] {snap[o]:#04x}->{post[o]:#04x}")
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[ENTRY] = at_entry; cpu.hook_names[ENTRY] = "dc1_entry"
    cpu.replacement_hooks[RET] = at_ret; cpu.hook_names[RET] = "dc1_ret"

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
    print("\n=== decode_input whole-routine shadow ===")
    for k in sorted(stats):
        print(f"  {k:24s} {stats[k]}")
    for m in mism:
        print("  " + m)
    ok = stats["BAD"] == 0 and stats["UNMODELED"] == 0 and not any(k.startswith("GAP:") for k in stats)
    print("\nDC1 decode_input:", "PASS (byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
