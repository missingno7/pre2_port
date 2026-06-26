"""Shadow-verify the recovered object-AI handlers byte-exact against the ASM, on a replayed demo.

This is the LIGHT-FOOTPRINT counterpart of ``probe_object_tick.py``: it installs only two hooks — the
handler dispatch (``68FC``) and the dispatch RET site (``6901``) — so it stays on exactly the same replay path
as ``demo_census.py``. The heavy ``probe_object_tick`` (17 hooks for the leaf routines) perturbs the emulated
schedule enough to DIVERGE on input-timing-sensitive demos (e.g. 111734), which silently changes which enemy
types spawn; this probe does not, so it is the authority for per-handler equivalence.

At each ``68FC`` it snapshots the object+def, runs the recovered handler on a copy, and at ``6901`` compares
every written field against the ASM's actual writes. Run:
    python pre2/probes/probe_handler_shadow.py [demo_dir] [handler_hex]   # optional filter to one handler
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
import pre2.recovered.object_update as ou
from pre2.probes.demo_census import HANDLER_ADDR

SEG = 0x1030
# handler-address -> recovered function (the CS:0x6AA9 dispatch targets we have recovered)
TARGETS = {0x7665: ou.handle_object_7665, 0x773D: ou.handle_object_773d, 0x77DE: ou.handle_object_77de,
           0x7C8C: ou.handle_object_7c8c, 0x7C90: ou.handle_object_7c90, 0x760F: ou.handle_object_760f,
           0x7C2D: ou.handle_object_7c2d, 0x7B91: ou.handle_object_7b91, 0x7ADF: ou.handle_object_7adf,
           0x7898: ou.handle_object_7898, 0x75C4: ou.handle_object_75c4, 0x78EC: ou.handle_object_78ec}

GSEG = 0x1A0F   # idx6 (78EC) reads/writes its shake + PRNG globals in this fixed data segment

_OBJ = (("x", 0), ("y", 2), ("id", 4), ("xvel", 8), ("yvel", 0xA), ("anim_ptr", 0xC))
_OBJB = (("state", 0xE),)   # [si+5] is the high byte of id [si+4] (overlap), not a separate field


def run(demo, only=None):
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
        o = {k: rdw(ds, si + off) for k, off in _OBJ}
        o.update({k: rdb(ds, si + off) for k, off in _OBJB})
        return o

    def defd(ds, d):
        return {"d2": rdw(ds, d + 2), "d4": rdb(ds, d + 4), "d6": rdb(ds, d + 6), "d7": rdb(ds, d + 7),
                "d9": rdw(ds, d + 9), "dB": rdw(ds, d + 0xB), "dD": rdb(ds, d + 0xD), "dE": rdb(ds, d + 0xE),
                "dF": rdb(ds, d + 0xF), "d10": rdb(ds, d + 0x10), "d11": rdb(ds, d + 0x11),
                "d12": rdb(ds, d + 0x12), "d13": rdb(ds, d + 0x13), "d14": rdb(ds, d + 0x14)}

    def shake_globals():       # idx6's mutable shake-state + PRNG state (seg 0x1A0F)
        return {"a30e": rdw(GSEG, 0xA30E), "a310": rdw(GSEG, 0xA310), "bc0": rdb(GSEG, 0x6BC0),
                "bc1": rdb(GSEG, 0x6BC1), "ror": rdw(GSEG, 0x28C1), "la": rdb(GSEG, 0x2CEC),
                "lb": rdb(GSEG, 0x2CED), "lc": rdb(GSEG, 0x2CEE), "ld": rdw(GSEG, 0x2CEF)}

    res = Counter()
    mism = []
    pend = [None]
    seen = Counter()

    def disp(c):
        bx, cs, ds, si = c.s.bx, c.s.cs, c.s.ds, c.s.si
        tgt = rdw(cs, (bx + 0x6AA9) & 0xFFFF)
        seen[tgt] += 1
        fn = TARGETS.get(tgt)
        if fn is not None and (only is None or tgt == only):
            d = rdw(ds, si + 6)
            o, df = objd(ds, si), defd(ds, d)
            glb = {"player_x": rdw(ds, 0x4F1C), "player_y": rdw(ds, 0x4F1E), "frame": rdb(ds, 0x6BD5),
                   "shake": rdb(ds, 0x6BEA), "a340": rdb(ds, 0xA340), "mode": rdb(ds, 0x2D8A)}

            def tile_prop(tx, ty):                     # the live level-map terrain lookup (idx3 needs it)
                tile = cpu.mem.data[((rdw(ds, 0x2DDA) << 4) + ((ty * 0x100 + tx) & 0xFFFF)) & 0xFFFFF]
                return rdb(ds, 0x7F5E + tile)

            pg = None
            try:
                if tgt == 0x7B91:
                    fn(o, df, glb, lambda off: rdw(ds, off), tile_prop=tile_prop)
                elif tgt == 0x7ADF:
                    fn(o, df, glb, lambda off: rdw(ds, off),
                       cos_table=lambda a: rdb(ds, (0x6F90 + a) & 0xFFFF),
                       sin_table=lambda a: rdb(ds, (0x7090 + a) & 0xFFFF))
                elif tgt == 0x78EC:                        # idx6: shake globals + PRNG state in glb
                    glb.update(shake_globals())
                    glb["bd0"] = rdb(ds, 0x6BD0)
                    fn(o, df, glb, lambda off: rdw(ds, off))
                    pg = {k: glb[k] for k in shake_globals()}   # predicted post-globals
                else:
                    fn(o, df, glb, lambda off: rdw(ds, off))
                pend[0] = (tgt, si, d, o, df, pg)
            except ou.ObjectScaleUnsupported:
                pend[0] = None
        interpret_current_instruction_without_hook(c)

    def exit_(c):
        if pend[0] is not None:
            tgt, si, d, po, pdf, pg = pend[0]
            pend[0] = None
            ds = c.s.ds
            ao, adf = objd(ds, si), defd(ds, d)
            ag = shake_globals() if pg is not None else None
            ok = (po == ao and pdf == adf and pg == ag)
            res[(tgt, "match" if ok else "MISMATCH")] += 1
            if not ok and len(mism) < 8:
                do = {k: (po[k], ao[k]) for k in po if po[k] != ao[k]}
                dd = {k: (pdf[k], adf[k]) for k in pdf if pdf[k] != adf[k]}
                dg = {k: (pg[k], ag[k]) for k in (pg or {}) if pg[k] != ag[k]}
                mism.append((f"{tgt:04X} si={si:#06x}", "obj", do, "def", dd, "glb", dg))
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(SEG, 0x68FC)] = disp
    cpu.hook_names[(SEG, 0x68FC)] = "shadow_disp"
    cpu.replacement_hooks[(SEG, 0x6901)] = exit_
    cpu.hook_names[(SEG, 0x6901)] = "shadow_exit"

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
    print("  dispatch:", " ".join(f"idx{HANDLER_ADDR.get(t, '?')}={n}" for t, n in sorted(seen.items())))
    for (tgt, k), n in sorted(res.items()):
        print(f"  {tgt:04X} {k}: {n}")
    for m in mism:
        print("   ", m)
    return res


def main():
    demo = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260626_111734"
    only = int(sys.argv[2], 16) if len(sys.argv) > 2 else None
    run(demo, only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
