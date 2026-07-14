"""Shadow-verify the recovered 2nd-pass walker handlers byte-exact vs the ASM, on a replayed demo.

Runs the demo as PURE ASM (native_replacements=False) so the real walker (1030:6913..698B) + its per-type
handlers (cs:[bx+0x6AC3]) execute. Observes at the dispatch CALL (6944) and its return (6949):

  * at 6944: read the entity (ds:si) + handler index ([si+1]), snapshot DS, and run the recovered
    `dispatch_handler` to PREDICT the {offset: (value,width)} writes.
  * at 6949: diff the predicted writes vs DS (must match), and report any DS byte the ASM changed that the
    recovered handler did NOT predict (excluding the timer-tick counter the IRQ may bump mid-handler).

Run:  python -m pre2.probes.probe_second_pass_handlers [demo_dir ...]
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
import pre2.recovered.object_inject as oi
import play

CS = 0x1030
DISPATCH = (CS, 0x6944)     # call cs:[bx+0x6ac3]
RET = (CS, 0x6949)          # pop ... (handler returned here)
CAM_X, CAM_Y = 0x2DE4, 0x2DE6
MAP_SEG_PTR = 0x2DDA        # es = [0x2DDA] (level map) for the player-trail terrain scan
_IRQ_IGNORE = {0x27EE, 0x27EF, 0x27F0, 0x27F1}   # timer counters (bumped by the INT 08 ISR if it fires mid-handler)


def _run(demo: str, max_frames: int, stats: Counter, mism: list):
    pb = InputDemoPlayback.load(demo)
    import argparse
    args = argparse.Namespace(exe=str(ROOT / "assets" / "pre2.exe"), game_root=str(ROOT / "assets"),
                              audio="off", fast_adlib=False, steps=None, no_replacements=True,
                              fast_retrace_waits=False)
    meta = pb.manifest.get("metadata", {})
    args.chunk_steps = int(meta.get("chunk_steps", 2142)); args.present_hz = int(meta.get("present_hz", 70))
    args.retrace_pulse = float(meta.get("retrace_pulse", 0.28)); args.timer_irq = bool(meta.get("timer_irq", True))
    args.input_irq_steps = int(meta.get("input_irq_steps", 2_000_000))
    rt = load_pre2_snapshot(args.exe, pb.snapshot_path(), game_root=args.game_root, native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False
    md = cpu.mem.data
    pending = {}

    def ds_base():
        return (cpu.s.ds << 4) & 0xFFFFF

    def at_dispatch(c):
        b = ds_base(); si = c.s.si
        rb = lambda o: md[(b + (o & 0xFFFF)) & 0xFFFFF]
        rw = lambda o: md[(b + (o & 0xFFFF)) & 0xFFFFF] | (md[(b + ((o + 1) & 0xFFFF)) & 0xFFFFF] << 8)
        idx = rb(si + 1) & 0x7F          # handler index (bit7 is the skip flag, masked off for dispatch)
        es = rw(MAP_SEG_PTR); eb = (es << 4) & 0xFFFFF
        read_es = lambda o: md[(eb + (o & 0xFFFF)) & 0xFFFFF]
        read_id = lambda slot: rw(oi.OBJ_BASE + slot * oi.OBJ_STRIDE + 4)
        find_free = lambda: oi.find_free_object_slot(read_id)
        try:
            writes, drawn = oi.dispatch_handler_bytes(idx, rb, rw, read_es, si, rw(CAM_X), rw(CAM_Y), find_free)
        except ValueError as e:
            stats[f"UNRECOVERED idx{idx}"] += 1
            interpret_current_instruction_without_hook(c); return
        # byte-level predicted writes
        pbytes = {}
        for off, (val, width) in writes.items():
            for k in range(width):
                pbytes[(off + k) & 0xFFFF] = (val >> (8 * k)) & 0xFF
        snap = bytes(md[b:b + 0x10000])
        pending["d"] = (idx, si, pbytes, snap, drawn)
        stats[f"idx{idx}"] += 1
        stats[f"idx{idx}:{'draw' if drawn else 'skip'}"] += 1
        interpret_current_instruction_without_hook(c)

    def at_ret(c):
        d = pending.pop("d", None)
        if d is not None:
            idx, si, pbytes, snap, drawn = d
            b = ds_base()
            post = md[b:b + 0x10000]
            # 1) every predicted write must match the ASM
            for off, val in pbytes.items():
                if post[off] != val:
                    if len(mism) < 30:
                        mism.append(f"idx{idx} si={si:#06x} PRED [{off:#06x}]={val:#04x} asm={post[off]:#04x}")
                    stats["MISMATCH"] += 1
                    break
            else:
                # 2) every ASM-changed byte must have been predicted (catch unmodeled writes)
                for off in range(0x10000):
                    if post[off] != snap[off] and off not in pbytes and off not in _IRQ_IGNORE:
                        if len(mism) < 30:
                            mism.append(f"idx{idx} si={si:#06x} UNMODELED [{off:#06x}] {snap[off]:#04x}->{post[off]:#04x}")
                        stats["UNMODELED"] += 1
                        break
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[DISPATCH] = at_dispatch; cpu.hook_names[DISPATCH] = "shadow_dispatch"
    cpu.replacement_hooks[RET] = at_ret; cpu.hook_names[RET] = "shadow_ret"

    det_speed = max(1, args.chunk_steps * max(1, args.present_hz))
    det_now = lambda: cpu.instruction_count / det_speed
    rt.dos.time_source = det_now
    tick = {"next": 0.0}
    frame = 0
    while not pb.finished(frame) and frame < max_frames:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=args.input_irq_steps))
        play._advance_demo_frame(rt, chunk_steps=args.chunk_steps, sub_batch=2000, clock=det_now,
                                 pic=rt.dos.pic, sound_blaster=None, timer_irq=args.timer_irq,
                                 input_irq_steps=args.input_irq_steps, tick_state=tick)
        frame += 1
    print(f"  {Path(demo).name}: {frame} frames")


def main():
    demos = sys.argv[1:] or [
        "artifacts/demo_pre2_20260627_213332",
        "artifacts/demo_pre2_20260626_190542",
        "artifacts/demo_pre2_20260626_115215",
    ]
    stats = Counter(); mism = []
    for d in demos:
        _run(d, max_frames=500, stats=stats, mism=mism)
    print("\n=== second-pass handler shadow ===")
    for k in sorted(stats):
        print(f"  {k:24s} {stats[k]}")
    if mism:
        print("\n--- first divergences ---")
        for m in mism:
            print("  " + m)
    ok = stats["MISMATCH"] == 0 and stats["UNMODELED"] == 0 and not any(k.startswith("UNRECOVERED") for k in stats)
    print("\nSECOND-PASS HANDLERS:", "PASS (byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
