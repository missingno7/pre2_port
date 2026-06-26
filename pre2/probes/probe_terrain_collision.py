"""Shadow-verify the recovered terrain-collision routine (1030:698C) byte-exact against the ASM on a demo.

The walker calls ``698C`` before the AI dispatch for any object whose def has ``[def+4]&8``. This probe hooks
the routine's entry (``698C``) and RET (``6A7C``), runs :func:`terrain_collision` on a copy at entry, and at
the RET compares the obj (x/y/xvel/yvel/anim_ptr) + ``[def+4]`` writes. The level map + the three property
tables are fed as live callbacks. Run:
    python pre2/probes/probe_terrain_collision.py [demo_dir]
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from play import _advance_frame_deterministic, _make_replay_runtime
from pre2.recovered.object_update import terrain_collision

SEG = 0x1030


def run(demo):
    pb = InputDemoPlayback.load(Path(demo))
    args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
                              timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250,
                              present_hz=120, retrace_pulse=0.06, verify=False)
    rt = _make_replay_runtime(args, pb)
    cpu = rt.cpu

    def rdw(ds, off):
        b = (ds << 4) & 0xFFFFF
        return cpu.mem.data[(b + (off & 0xFFFF)) & 0xFFFFF] | (cpu.mem.data[(b + ((off + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def rdb(ds, off):
        return cpu.mem.data[((ds << 4) + (off & 0xFFFF)) & 0xFFFFF]

    def objd(ds, si):
        return {"x": rdw(ds, si), "y": rdw(ds, si + 2), "xvel": rdw(ds, si + 8),
                "yvel": rdw(ds, si + 0xA), "anim_ptr": rdw(ds, si + 0xC)}

    res = Counter()
    mism = []
    pend = [None]

    def pre(c):
        ds, si = c.s.ds, c.s.si
        d = rdw(ds, si + 6)
        o, df = objd(ds, si), {"d4": rdb(ds, d + 4)}
        mapseg = rdw(ds, 0x2DDA)
        terrain_collision(o, df, read_map=lambda i: rdb(mapseg, i),
                          prop_a=lambda t: rdb(ds, 0x7E5E + t), prop_b=lambda t: rdb(ds, 0x7F5E + t),
                          slope=lambda t: rdb(ds, 0x8E1D + t), read_word=lambda off: rdw(ds, off))
        pend[0] = (si, d, o, df)
        interpret_current_instruction_without_hook(c)

    def post(c):
        if pend[0] is not None:
            si, d, po, pdf = pend[0]
            pend[0] = None
            ds = c.s.ds
            ao, adf = objd(ds, si), {"d4": rdb(ds, d + 4)}
            ok = (po == ao and pdf == adf)
            res["match" if ok else "MISMATCH"] += 1
            if not ok and len(mism) < 10:
                do = {k: (hex(po[k]), hex(ao[k])) for k in po if po[k] != ao[k]}
                dd = {k: (hex(pdf[k]), hex(adf[k])) for k in pdf if pdf[k] != adf[k]}
                mism.append((f"si={si:#06x}", do, dd))
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(SEG, 0x698C)] = pre
    cpu.hook_names[(SEG, 0x698C)] = "terrain_pre"
    cpu.replacement_hooks[(SEG, 0x6A7C)] = post
    cpu.hook_names[(SEG, 0x6A7C)] = "terrain_post"

    det_speed = max(1, args.chunk_steps * args.present_hz)
    det_now = lambda: cpu.instruction_count / det_speed   # noqa: E731
    rt.dos.time_source = det_now
    ts = {"next": 0.0}
    frame = 0
    while not pb.finished(frame):
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=args.input_irq_steps))
        _advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000, clock=det_now,
                                     pic=rt.dos.pic, sound_blaster=None, timer_irq=args.timer_irq,
                                     input_irq_steps=args.input_irq_steps, tick_state=ts, det_speed=det_speed)
        frame += 1

    print(f"{demo}  frames={frame}")
    for k, n in sorted(res.items()):
        print(f"  {k}: {n}")
    for m in mism:
        print("   ", m)
    return res


def main():
    run(sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260626_112253")
    return 0


if __name__ == "__main__":
    sys.exit(main())
