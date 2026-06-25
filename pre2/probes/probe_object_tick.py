"""Stage 0/1 object-system observation probe (OBSERVE-ONLY, never mutates live state).

Installs chaining observe-hooks around the recovered walker boundary (disasm-confirmed) and replays a demo:
  0x6856  loop top         -> enumerate the 12 update slots: live (non-empty + changing) vs stale.
  0x6861  velocity-apply   -> snapshot record + PREDICT post X/Y (shadow recovery of 6861..6873).
  0x6875  post-velocity    -> compare predicted vs ACTUAL X/Y delta (the shadow verification).
  0x68FC  handler dispatch -> map object handler-index -> handler address (the per-type AI table @cs:0x6AA9).

Answers: which slots are live, which routine writes x/y, whether the velocity formula is exact, and the
handler dispatch map. Run: python pre2/probes/probe_object_tick.py [demo_dir]
"""
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from play import _advance_frame_deterministic, _make_replay_runtime
from pre2.recovered.object_update import apply_velocity   # SHADOW: the recovered routine under test

SEG = 0x1030


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


def main():
    demo = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260626_001513"
    playback = InputDemoPlayback.load(Path(demo))
    args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
                              timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250,
                              present_hz=120, retrace_pulse=0.06, verify=False)
    rt = _make_replay_runtime(args, playback)
    cpu = rt.cpu

    def rdw(ds, off):
        l = ((ds << 4) + off) & 0xFFFFF
        return cpu.mem.data[l] | (cpu.mem.data[l + 1] << 8)

    def rec_fields(ds, si):
        return {k: rdw(ds, si + o) for k, o in
                (("x", 0), ("y", 2), ("id", 4), ("def", 6), ("xv", 8), ("yv", 0xA), ("anim", 0xC))}

    # state
    slot_seen = Counter()         # slot_index -> ticks non-empty
    slot_moved = Counter()        # slot_index -> ticks position changed
    slot_prev_pos = {}            # slot_index -> (x,y)
    pending = {}                  # si -> predicted (x,y) from 6861
    vel = Counter()               # 'match' / 'mismatch'
    vel_moving = Counter()        # among objects with a nonzero predicted delta
    vel_static = Counter()        # among objects with zero predicted delta
    mismatches = []
    handlers = defaultdict(Counter)   # handler_index -> Counter(target_addr -> n)
    handler_ids = defaultdict(Counter)  # handler_index -> Counter(base sprite id)

    def chain(c):
        interpret_current_instruction_without_hook(c)

    def h_looptop(c):
        ds, si, bp = c.s.ds, c.s.si, c.s.bp
        slot = (0xC - bp) & 0xFF
        if rdw(ds, si + 4) != 0xFFFF:
            slot_seen[slot] += 1
            pos = (rdw(ds, si), rdw(ds, si + 2))
            if slot in slot_prev_pos and slot_prev_pos[slot] != pos:
                slot_moved[slot] += 1
            slot_prev_pos[slot] = pos
        chain(c)

    def h_vel_pre(c):
        ds, si = c.s.ds, c.s.si
        f = rec_fields(ds, si)
        nx, ny = apply_velocity(f["x"], f["y"], f["xv"], f["yv"])   # SHADOW: recovered prediction
        pending[si] = (nx, ny, f["x"], f["y"])
        chain(c)

    def h_vel_post(c):
        ds, si = c.s.ds, c.s.si
        if si in pending:
            nx, ny, ox, oy = pending.pop(si)
            ax, ay = rdw(ds, si), rdw(ds, si + 2)
            ok = (ax == nx and ay == ny)
            vel["match" if ok else "mismatch"] += 1
            moved = (nx != ox or ny != oy)
            (vel_moving if moved else vel_static)["match" if ok else "mismatch"] += 1
            if not ok and len(mismatches) < 10:
                mismatches.append((si, (ox, oy), "pred", (nx, ny), "actual", (ax, ay)))
        chain(c)

    def h_dispatch(c):
        bx, cs, ds, si = c.s.bx, c.s.cs, c.s.ds, c.s.si
        target = rdw(cs, (bx + 0x6AA9) & 0xFFFF)
        idx = bx >> 1
        handlers[idx][target] += 1
        handler_ids[idx][rdw(ds, si + 4) & 0x1FFF] += 1
        chain(c)

    for off, fn in ((0x6856, h_looptop), (0x6861, h_vel_pre), (0x6875, h_vel_post), (0x68FC, h_dispatch)):
        cpu.replacement_hooks[(SEG, off)] = fn
        cpu.hook_names[(SEG, off)] = f"probe_{off:04x}"

    det_speed = max(1, int(args.chunk_steps) * max(1, int(args.present_hz)))
    det_now = lambda: cpu.instruction_count / det_speed   # noqa: E731
    rt.dos.time_source = det_now
    tick_state = {"next": 0.0}
    frame = 0
    while not playback.finished(frame):
        playback.apply_to_runtime(frame, rt,
                                  deliver=lambda runtime, sc: deliver_scancode(runtime, sc,
                                                                               max_steps=args.input_irq_steps))
        try:
            _advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000,
                                         clock=det_now, pic=rt.dos.pic, sound_blaster=None,
                                         timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                         tick_state=tick_state, det_speed=det_speed)
        except Exception as e:
            print(f"stopped at frame {frame}: {type(e).__name__}: {e}")
            break
        frame += 1

    print(f"demo: {demo}   frames: {frame}\n")
    print("=== UPDATE SLOTS (0x4FD0, 12 x 0x12) — live vs stale ===")
    print(f"  {'slot':>4} {'non-empty ticks':>16} {'moved ticks':>12}")
    for s in range(12):
        print(f"  {s:>4} {slot_seen[s]:>16} {slot_moved[s]:>12}"
              + ("   <- live" if slot_moved[s] else ("   (static/empty)" if slot_seen[s] == 0 else "   (non-moving)")))
    print(f"\n=== VELOCITY-APPLY SHADOW (0x6861..0x6873: Y+=sar(yv,4); if xv!=FFFF X+=sar(xv,4)) ===")
    tot = vel["match"] + vel["mismatch"]
    print(f"  checks: {tot}   match: {vel['match']}   MISMATCH: {vel['mismatch']}")
    print(f"  among MOVING objects: match={vel_moving['match']} mismatch={vel_moving['mismatch']}")
    print(f"  among STATIC objects: match={vel_static['match']} mismatch={vel_static['mismatch']}")
    for m in mismatches:
        print(f"    MISMATCH {m}")
    print(f"\n=== AI HANDLER DISPATCH (0x68FC: call cs:[bx+0x6AA9]) ===")
    print(f"  {'idx':>4} {'handler@':>9} {'calls':>8}  base sprite-ids (top)")
    for idx in sorted(handlers):
        tgt, n = handlers[idx].most_common(1)[0]
        ids = ",".join(f"{i:#06x}" for i, _ in handler_ids[idx].most_common(4))
        print(f"  {idx:>4} {tgt:>9X} {n:>8}  {ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
