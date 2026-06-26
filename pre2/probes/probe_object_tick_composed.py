"""Whole-tick shadow: verify the COMPOSED ``object_tick`` against the ASM walker (1030:684E..6913) on a demo.

At the walker entry (``684E``) it snapshots the object data segment into a frozen image, runs the recovered
:func:`pre2.recovered.object_tick.object_tick` on that image, and at the loop exit (``6913``) compares — for
every slot that was non-empty at tick start — the acting object's full record + its def, plus the shared shake/
PRNG globals, against the ASM's evolved memory. (Slots that were EMPTY at start but get spawned into mid-tick
are skipped: ``object_tick`` does not model the spawn emitters, which never touch an acting object's own
record — so per-slot reproduction stays byte-exact.) Run:
    python pre2/probes/probe_object_tick_composed.py [demo_dir]
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
from pre2.recovered.object_tick import OBJ_BASE, OBJ_COUNT, OBJ_STRIDE, Pre2ObjectGap, object_tick
from pre2.recovered.object_tick import _def_view, _obj_view

SEG = 0x1030


class LiveMem:
    """A frozen copy of the object data segment (`ds` == 0x1A0F) presented as a WalkerMem. Map tiles + the
    handler table live in other segments and are read straight from the live VM (read-only during a tick)."""

    def __init__(self, cpu, ds, cs):
        base = (ds << 4) & 0xFFFFF
        self.img = bytearray(cpu.mem.data[base:base + 0x10000])
        self.cpu, self.cs = cpu, cs
        self.map_seg = self.rw(0x2DDA)

    def rb(self, off):
        return self.img[off & 0xFFFF]

    def rw(self, off):
        o = off & 0xFFFF
        return self.img[o] | (self.img[(o + 1) & 0xFFFF] << 8)

    def wb(self, off, v):
        self.img[off & 0xFFFF] = v & 0xFF

    def ww(self, off, v):
        o = off & 0xFFFF
        self.img[o] = v & 0xFF
        self.img[(o + 1) & 0xFFFF] = (v >> 8) & 0xFF

    def read_map(self, idx):
        return self.cpu.mem.data[(((self.map_seg << 4) & 0xFFFFF) + (idx & 0xFFFF)) & 0xFFFFF]

    def prop_a(self, t):
        return self.rb(0x7E5E + t)

    def prop_b(self, t):
        return self.rb(0x7F5E + t)

    def slope(self, t):
        return self.rb(0x8E1D + t)

    def cos_table(self, a):
        return self.rb((0x6F90 + a) & 0xFFFF)

    def sin_table(self, a):
        return self.rb((0x7090 + a) & 0xFFFF)

    def tile_prop(self, tx, ty):
        return self.rb(0x7F5E + self.read_map((ty * 0x100 + tx) & 0xFFFF))

    def scale(self):
        return self.rw(0x6BE2)

    def handler_addr(self, tbl):       # tbl = the 8-bit table byte-offset the walker computes (shl bl,1)
        b = (self.cs << 4) & 0xFFFFF
        off = (tbl + 0x6AA9) & 0xFFFF
        return self.cpu.mem.data[(b + off) & 0xFFFFF] | (self.cpu.mem.data[(b + ((off + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def glb(self):
        return {"player_x": self.rw(0x4F1C), "player_y": self.rw(0x4F1E), "frame": self.rb(0x6BD5),
                "shake": self.rb(0x6BEA), "a340": self.rb(0xA340), "mode": self.rb(0x2D8A),
                "a30e": self.rw(0xA30E), "a310": self.rw(0xA310), "bc0": self.rb(0x6BC0), "bc1": self.rb(0x6BC1),
                "bd0": self.rb(0x6BD0), "ror": self.rw(0x28C1), "la": self.rb(0x2CEC), "lb": self.rb(0x2CED),
                "lc": self.rb(0x2CEE), "ld": self.rw(0x2CEF)}

    def write_glb(self, g):
        self.ww(0xA30E, g["a30e"]); self.ww(0xA310, g["a310"]); self.wb(0x6BC0, g["bc0"])
        self.wb(0x6BC1, g["bc1"]); self.ww(0x28C1, g["ror"]); self.wb(0x2CEC, g["la"])
        self.wb(0x2CED, g["lb"]); self.wb(0x2CEE, g["lc"]); self.ww(0x2CEF, g["ld"])


def run(demo):
    pb = InputDemoPlayback.load(Path(demo))
    args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
                              timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250,
                              present_hz=120, retrace_pulse=0.06, verify=False)
    rt = _make_replay_runtime(args, pb)
    cpu = rt.cpu
    res = Counter()
    mism = []
    pend = [None]

    def entry(c):
        ds, cs = c.s.ds, c.s.cs
        try:
            pred = LiveMem(cpu, ds, cs)
            tracked = [(s, OBJ_BASE + s * OBJ_STRIDE, pred.rw(OBJ_BASE + s * OBJ_STRIDE + 6))
                       for s in range(OBJ_COUNT) if pred.rw(OBJ_BASE + s * OBJ_STRIDE + 4) != 0xFFFF]
            object_tick(pred)
            pend[0] = (ds, cs, pred, tracked)
            res["ticks"] += 1
        except Pre2ObjectGap:
            pend[0] = None
            res["gap_ticks"] += 1
        interpret_current_instruction_without_hook(c)

    def exit_(c):
        if pend[0] is not None:
            ds, cs, pred, tracked = pend[0]
            pend[0] = None
            post = LiveMem(cpu, ds, cs)
            for slot, si, d in tracked:
                po, ao = _obj_view(pred, si), _obj_view(post, si)
                pdf, adf = _def_view(pred, d), _def_view(post, d)
                ok = (po == ao and pdf == adf)
                res["slot_match" if ok else "slot_MISMATCH"] += 1
                if not ok and len(mism) < 10:
                    do = {k: (hex(po[k]), hex(ao[k])) for k in po if po[k] != ao[k]}
                    dd = {k: (hex(pdf[k]), hex(adf[k])) for k in pdf if pdf[k] != adf[k]}
                    mism.append((f"slot {slot} si={si:#06x}", do, dd))
            pg, ag = pred.glb(), post.glb()
            gok = (pg == ag)
            res["glb_match" if gok else "glb_MISMATCH"] += 1
            if not gok and len(mism) < 12:
                mism.append(("globals", {k: (hex(pg[k]), hex(ag[k])) for k in pg if pg[k] != ag[k]}))
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(SEG, 0x684E)] = entry
    cpu.hook_names[(SEG, 0x684E)] = "tick_entry"
    cpu.replacement_hooks[(SEG, 0x6913)] = exit_
    cpu.hook_names[(SEG, 0x6913)] = "tick_exit"

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
    for k in ("ticks", "gap_ticks", "slot_match", "slot_MISMATCH", "glb_match", "glb_MISMATCH"):
        if res[k]:
            print(f"  {k}: {res[k]}")
    for m in mism:
        print("   ", m)
    return res


def main():
    run(sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260626_112253")
    return 0


if __name__ == "__main__":
    sys.exit(main())
