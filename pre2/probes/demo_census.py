"""Census which object-handler types each recorded demo exercises (replays each demo, tallies the 68FC
dispatch by handler address). Use it to pick the demo that witnesses a given enemy type. Run:
    python pre2/probes/demo_census.py            # all demos under artifacts/
    python pre2/probes/demo_census.py <demo_dir> # one demo
"""
import argparse
import glob
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from play import _advance_frame_deterministic, _make_replay_runtime

# handler-address -> index, from the CS:0x6AA9 dispatch table (24 entries 0..23).
HANDLER_ADDR = {0x7c90: 0, 0x7c8c: 1, 0x7c2d: 2, 0x7b91: 3, 0x7adf: 4, 0x7a60: 5, 0x78ec: 6, 0x7898: 7,
                0x77de: 8, 0x773d: 9, 0x7665: 10, 0x760f: 11, 0x75c4: 12, 0x7f6c: 13, 0x7f26: 14, 0x7ee2: 15,
                0x7ed8: 16, 0x7ebf: 17, 0x7eb5: 18, 0x7e97: 22, 0x7d9b: 23}
RECOVERED = {0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12}  # handle_object_* in pre2.recovered.object_update
_MAX_FRAMES = 2000


def census(demo):
    pb = InputDemoPlayback.load(Path(demo))
    args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
                              timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250,
                              present_hz=120, retrace_pulse=0.06, verify=False)
    rt = _make_replay_runtime(args, pb)
    cpu = rt.cpu
    seen = Counter()

    def h(c):
        bx, cs = c.s.bx, c.s.cs
        b = (cs << 4) & 0xFFFFF
        tgt = cpu.mem.data[(b + ((bx + 0x6AA9) & 0xFFFF)) & 0xFFFFF] \
            | (cpu.mem.data[(b + ((bx + 0x6AAA) & 0xFFFF)) & 0xFFFFF] << 8)
        seen[tgt] += 1
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(0x1030, 0x68FC)] = h
    cpu.hook_names[(0x1030, 0x68FC)] = "census"
    det_speed = max(1, args.chunk_steps * args.present_hz)
    det_now = lambda: cpu.instruction_count / det_speed   # noqa: E731
    rt.dos.time_source = det_now
    ts = {"next": 0.0}
    frame = 0
    while not pb.finished(frame) and frame < _MAX_FRAMES:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=args.input_irq_steps))
        _advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000, clock=det_now,
                                     pic=rt.dos.pic, sound_blaster=None, timer_irq=args.timer_irq,
                                     input_irq_steps=args.input_irq_steps, tick_state=ts, det_speed=det_speed)
        frame += 1
    return seen


def main():
    demos = [sys.argv[1]] if len(sys.argv) > 1 else sorted(glob.glob("artifacts/demo_pre2_*"))
    union = set()
    for d in demos:
        try:
            s = census(d)
        except Exception as e:
            print(f"  {os.path.basename(d)}: ERR {type(e).__name__}: {e}")
            continue
        idxs = sorted({HANDLER_ADDR.get(t, -1) for t in s})
        union |= {i for i in idxs if i >= 0}
        lbl = " ".join((f"idx{i}{'' if i in RECOVERED else '*'}" if i >= 0 else "idx?") for i in idxs)
        unk = [f"{t:04x}" for t in s if t not in HANDLER_ADDR]
        print(f"  {os.path.basename(d):34s} {lbl}" + (f"  UNMAPPED:{unk}" if unk else ""))
    if len(demos) > 1:
        miss = sorted(set(range(24)) - union)
        print(f"\n  (* = not yet recovered)   witnessed indices: {sorted(union)}")
        print(f"  NEVER witnessed in any demo: {miss}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
