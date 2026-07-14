"""Shadow-verify the COMPOSED 2nd-pass walker (second_pass_tick) byte-exact vs the ASM, on a replayed demo.

Pure ASM replay (native_replacements=False). At the 2nd-pass entry (6913) predict the WHOLE pass with the
recovered `second_pass_tick` over a copy-overlay (no real writes); step aside so the ASM runs 6913..698B; at
the return (698B) diff the predicted DS state vs the actual DS (minus the timer-ISR counter that an INT 08
mid-pass may bump). This proves the list-walk + skip predicates + per-type dispatch + anim-frame resolve +
stride advance collapse to native byte-for-byte.

Run:  python -m pre2.probes.probe_second_pass_tick [demo_dir ...]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot
import pre2.recovered.object_inject as oi
import play

CS = 0x1030
TICK_ENTRY = (CS, 0x6913)
TICK_RET = (CS, 0x698B)
CAM_X, CAM_Y, MAP_SEG_PTR = 0x2DE4, 0x2DE6, 0x2DDA
# The 2nd pass writes ONLY these regions; the INT 08 ISR's async writes (a counter at ~0x27xx + the PC-speaker
# sound descriptor roaming a low music table) land outside them, so scanning the OWNED set for unpredicted
# writes is ISR-noise-free. (Per-handler whole-DS footprint already validated by probe_second_pass_handlers.)
_OWNED = (frozenset(range(0x4FD0, 0x50F0))            # object record list (12 * 0x12)
          | frozenset(range(0x8489, 0x8800))          # the 2nd-pass entity list
          | {0xA32E, 0xA32F, 0xA341, 0xA342, 0x6BCC}  # render-ptr, trail-offset ring, aura toggle
          | frozenset(range(0x2CEC, 0x2CF2)))         # rng_lcg state


class _Ov:
    """Read-through copy-overlay of the 64KB DS at pass entry; accumulates the recovered writes."""
    def __init__(self, snap):
        self.snap = snap
        self.w = {}

    def rb(self, o):
        o &= 0xFFFF
        return self.w.get(o, self.snap[o])

    def rw(self, o):
        return self.rb(o) | (self.rb((o + 1) & 0xFFFF) << 8)

    def apply(self, writes):
        for off, (val, width) in writes.items():
            for k in range(width):
                self.w[(off + k) & 0xFFFF] = (val >> (8 * k)) & 0xFF


def _run(demo, max_frames, stats, mism):
    pb = InputDemoPlayback.load(demo)
    meta = pb.manifest.get("metadata", {})
    args = argparse.Namespace(exe=str(ROOT / "assets" / "pre2.exe"), game_root=str(ROOT / "assets"),
                              audio="off", steps=None, no_replacements=True, fast_retrace_waits=False,
                              chunk_steps=int(meta.get("chunk_steps", 2142)),
                              present_hz=int(meta.get("present_hz", 70)),
                              timer_irq=bool(meta.get("timer_irq", True)),
                              input_irq_steps=int(meta.get("input_irq_steps", 2_000_000)))
    rt = load_pre2_snapshot(args.exe, pb.snapshot_path(), game_root=args.game_root, native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False
    md = cpu.mem.data
    pend = {}

    def at_entry(c):
        b = (c.s.ds << 4) & 0xFFFFF
        snap = bytes(md[b:b + 0x10000])
        ov = _Ov(snap)
        es = ov.rw(MAP_SEG_PTR); eb = (es << 4) & 0xFFFFF
        read_es = lambda o: md[(eb + (o & 0xFFFF)) & 0xFFFFF]
        try:
            oi.second_pass_tick_bytes(ov.rb, ov.rw, ov.apply, read_es, ov.rw(CAM_X), ov.rw(CAM_Y))
        except ValueError as e:
            stats[f"UNRECOVERED {e}"] += 1
            interpret_current_instruction_without_hook(c); return
        pend["d"] = (b, snap, ov.w)
        stats["ticks"] += 1
        interpret_current_instruction_without_hook(c)

    def at_ret(c):
        d = pend.pop("d", None)
        if d is not None:
            b, snap, w = d
            post = md[b:b + 0x10000]
            bad = None
            for off, val in w.items():                       # predicted writes must match the ASM
                if post[off] != val:
                    bad = f"PRED [{off:#06x}]={val:#04x} asm={post[off]:#04x}"; break
            if bad is None:                                  # every OWNED byte the ASM changed must be predicted
                for off in _OWNED:
                    if post[off] != snap[off] and off not in w:
                        bad = f"UNMODELED [{off:#06x}] {snap[off]:#04x}->{post[off]:#04x}"; break
            if bad is not None:
                stats["DIVERGENT"] += 1
                if len(mism) < 25:
                    mism.append(bad)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[TICK_ENTRY] = at_entry; cpu.hook_names[TICK_ENTRY] = "tick_shadow_entry"
    cpu.replacement_hooks[TICK_RET] = at_ret; cpu.hook_names[TICK_RET] = "tick_shadow_ret"

    det_speed = max(1, args.chunk_steps * max(1, args.present_hz))
    det_now = lambda: cpu.instruction_count / det_speed
    rt.dos.time_source = det_now
    tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < max_frames:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=args.input_irq_steps))
        play._advance_demo_frame(rt, chunk_steps=args.chunk_steps, sub_batch=2000, clock=det_now,
                                 pic=rt.dos.pic, sound_blaster=None, timer_irq=args.timer_irq,
                                 input_irq_steps=args.input_irq_steps, tick_state=tick)
        frame += 1
    print(f"  {Path(demo).name}: {frame} frames")


def main():
    demos = sys.argv[1:] or ["artifacts/demo_pre2_20260627_213332",
                             "artifacts/demo_pre2_20260627_190542",
                             "artifacts/demo_pre2_20260626_115215"]
    stats = Counter(); mism = []
    for d in demos:
        _run(d, 500, stats, mism)
    print("\n=== composed second_pass_tick shadow ===")
    for k in sorted(stats):
        print(f"  {k:22s} {stats[k]}")
    for m in mism:
        print("  " + m)
    ok = stats["DIVERGENT"] == 0 and not any(k.startswith("UNRECOVERED") for k in stats)
    print("\nSECOND_PASS_TICK:", "PASS (byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
