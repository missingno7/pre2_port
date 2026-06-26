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
from pre2.recovered.object_update import (AnimResult, DespawnResult,   # SHADOW: routines under test
                                          ObjectScaleUnsupported, advance_animation, apply_velocity,
                                          despawn_check, on_screen_tile,
                                          anim_script_rewind, anim_script_forward, handle_object_7665)

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

    def rdb(ds, off):
        return cpu.mem.data[((ds << 4) + (off & 0xFFFF)) & 0xFFFFF]

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
    anim = Counter()              # 'match' / 'mismatch' / 'scale_skip'
    anim_pending = {}             # si -> predicted AnimResult from 6881
    anim_mismatches = []
    dsp = Counter()               # 'match' / 'mismatch' / 'kept' / 'despawn'
    dsp_pending = [None]          # (si, def_ptr, predicted DespawnResult); 8084 is atomic -> single slot
    dsp_mismatches = []
    osc = Counter()               # on_screen_tile (8022): 'match' / 'mismatch'
    osc_pending = [None]          # predicted on_screen bool
    seek = Counter()              # anim rewind/forward (8048/8058): 'match' / 'mismatch'
    seek_pending = [None]         # predicted new script ptr
    handlers = defaultdict(Counter)   # handler_index -> Counter(target_addr -> n)
    handler_ids = defaultdict(Counter)  # handler_index -> Counter(base sprite id)
    hdl = Counter()               # handle_object_7665: 'match' / 'mismatch'
    hdl_pending = [None]          # (si, def_ptr, predicted obj dict, predicted def dict)
    hdl_mismatches = []
    _OBJ = (("x", 0), ("y", 2), ("id", 4), ("xvel", 8), ("yvel", 0xA), ("anim_ptr", 0xC))
    _OBJB = (("state", 0xE),)   # NOTE: [si+5] is the high byte of id [si+4] (overlaps) -> not a separate field

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

    def h_anim_pre(c):
        ds, si = c.s.ds, c.s.si
        script_ptr = rdw(ds, si + 0xC)
        old_id = rdw(ds, si + 4)
        flip = cpu.mem.data[((ds << 4) + ((si + 9) & 0xFFFF)) & 0xFFFFF]
        scale = rdw(ds, 0x6BE2)
        try:
            anim_pending[si] = advance_animation(script_ptr, lambda o: rdw(ds, o), old_id, flip, scale)
        except ObjectScaleUnsupported:
            anim_pending[si] = "scale"
            anim["scale_skip"] += 1
        chain(c)

    def h_anim_post(c):
        ds, si = c.s.ds, c.s.si
        if si in anim_pending:
            pred = anim_pending.pop(si)
            if pred == "scale":
                return chain(c)
            actual = AnimResult(sprite_id=rdw(ds, si + 4), script_ptr=rdw(ds, si + 0xC),
                                attr_a340=cpu.mem.data[((ds << 4) + 0xA340) & 0xFFFFF])
            ok = (pred == actual)
            anim["match" if ok else "mismatch"] += 1
            if not ok and len(anim_mismatches) < 10:
                anim_mismatches.append((si, "pred", repr(pred), "actual", repr(actual)))
        chain(c)

    def h_despawn_pre(c):
        ds, si = c.s.ds, c.s.si
        d = rdw(ds, si + 6)
        pred = despawn_check(rdw(ds, si), rdw(ds, si + 2), rdb(ds, si + 0xE), rdb(ds, si + 5), rdw(ds, si + 4),
                             rdw(ds, 0x4F1C), rdw(ds, 0x4F1E), rdw(ds, d + 2), rdb(ds, d + 4), rdb(ds, d + 7))
        dsp_pending[0] = (si, d, pred)
        dsp["kept" if pred.kept else "despawn"] += 1
        chain(c)

    def h_despawn_exit(c):
        if dsp_pending[0] is not None:
            ds = c.s.ds
            si, d, pred = dsp_pending[0]
            dsp_pending[0] = None
            actual = DespawnResult(pred.kept, rdw(ds, si + 4), rdw(ds, d + 2), rdb(ds, d + 4), rdb(ds, d + 7))
            ok = (pred == actual)
            dsp["match" if ok else "mismatch"] += 1
            if not ok and len(dsp_mismatches) < 10:
                dsp_mismatches.append((si, "pred", repr(pred), "actual", repr(actual)))
        chain(c)

    def h_onscreen_pre(c):
        ds = c.s.ds
        osc_pending[0] = on_screen_tile(c.s.ax, c.s.dx, rdw(ds, 0x2DE4), rdw(ds, 0x2DE6))
        chain(c)

    def h_onscreen_exit(actual):
        def h(c):
            if osc_pending[0] is not None:
                osc["match" if osc_pending[0] == actual else "mismatch"] += 1
                osc_pending[0] = None
            chain(c)
        return h

    def h_seek_pre(fn):
        def h(c):
            ds, si = c.s.ds, c.s.si
            try:
                seek_pending[0] = fn(rdw(ds, si + 0xC), lambda o: rdw(ds, o))
            except ObjectScaleUnsupported:
                seek_pending[0] = None
            chain(c)
        return h

    def h_seek_exit(c):
        if seek_pending[0] is not None:
            ds, si = c.s.ds, c.s.si
            seek["match" if rdw(ds, si + 0xC) == seek_pending[0] else "mismatch"] += 1
            seek_pending[0] = None
        chain(c)

    def _obj_dict(ds, si):
        o = {k: rdw(ds, si + off) for k, off in _OBJ}
        o.update({k: rdb(ds, si + off) for k, off in _OBJB})
        return o

    def _def_dict(ds, d):
        return {"d2": rdw(ds, d + 2), "d4": rdb(ds, d + 4), "d7": rdb(ds, d + 7), "dD": rdb(ds, d + 0xD)}

    def h_dispatch(c):
        bx, cs, ds, si = c.s.bx, c.s.cs, c.s.ds, c.s.si
        target = rdw(cs, (bx + 0x6AA9) & 0xFFFF)
        idx = bx >> 1
        handlers[idx][target] += 1
        handler_ids[idx][rdw(ds, si + 4) & 0x1FFF] += 1
        if target == 0x7665:                       # SHADOW the recovered idx10 handler
            d = rdw(ds, si + 6)
            obj, defn = _obj_dict(ds, si), _def_dict(ds, d)
            glb = {"mode": rdb(ds, 0x2D8A), "shake": rdb(ds, 0x6BEA), "a340": rdb(ds, 0xA340),
                   "frame": rdb(ds, 0x6BD5), "player_x": rdw(ds, 0x4F1C), "player_y": rdw(ds, 0x4F1E)}
            try:
                handle_object_7665(obj, defn, glb, lambda off: rdw(ds, off))
                hdl_pending[0] = (si, d, obj, defn)
            except ObjectScaleUnsupported:
                hdl_pending[0] = None
        chain(c)

    def h_handler_exit(c):
        if hdl_pending[0] is not None:
            ds = c.s.ds
            si, d, pobj, pdefn = hdl_pending[0]
            hdl_pending[0] = None
            aobj, adefn = _obj_dict(ds, si), _def_dict(ds, d)
            ok = (pobj == aobj and pdefn == adefn)
            hdl["match" if ok else "mismatch"] += 1
            if not ok and len(hdl_mismatches) < 8:
                do = {k: (pobj[k], aobj[k]) for k in pobj if pobj[k] != aobj[k]}
                dd = {k: (pdefn[k], adefn[k]) for k in pdefn if pdefn[k] != adefn[k]}
                hdl_mismatches.append((f"si={si:#06x}", "obj", do, "def", dd))
        chain(c)

    for off, fn in ((0x6856, h_looptop), (0x6861, h_vel_pre), (0x6875, h_vel_post),
                    (0x6881, h_anim_pre), (0x68E9, h_anim_post), (0x68FC, h_dispatch),
                    (0x8084, h_despawn_pre), (0x80CA, h_despawn_exit), (0x7D1A, h_despawn_exit),
                    (0x8022, h_onscreen_pre), (0x8044, h_onscreen_exit(True)), (0x8046, h_onscreen_exit(False)),
                    (0x8048, h_seek_pre(anim_script_rewind)), (0x8057, h_seek_exit),
                    (0x8058, h_seek_pre(anim_script_forward)), (0x806B, h_seek_exit),
                    (0x6901, h_handler_exit)):
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
    print(f"\n=== ANIM-ADVANCE SHADOW (0x6881..0x68E6: script walk -> [si+4] frame, [si+0xC] ptr, [0xA340]) ===")
    atot = anim["match"] + anim["mismatch"]
    print(f"  checks: {atot}   match: {anim['match']}   MISMATCH: {anim['mismatch']}"
          f"   scale-skip([0x6BE2]!=0): {anim['scale_skip']}")
    for m in anim_mismatches:
        print(f"    MISMATCH {m}")
    print(f"\n=== DESPAWN-IF-FAR SHADOW (0x8084 + 7CFF: keep / despawn [si+4],[def+2/4/7]) ===")
    dtot = dsp["match"] + dsp["mismatch"]
    print(f"  checks: {dtot}   match: {dsp['match']}   MISMATCH: {dsp['mismatch']}"
          f"   (kept: {dsp['kept']}  despawn: {dsp['despawn']})")
    for m in dsp_mismatches:
        print(f"    MISMATCH {m}")
    print(f"\n=== ON-SCREEN-TILE SHADOW (0x8022: pixel -> visible tile window vs camera) ===")
    otot = osc["match"] + osc["mismatch"]
    print(f"  checks: {otot}   match: {osc['match']}   MISMATCH: {osc['mismatch']}")
    stot = seek["match"] + seek["mismatch"]
    print(f"\n=== ANIM-SEEK SHADOW (0x8048 rewind / 0x8058 forward -> [si+0xC]) ===")
    print(f"  checks: {stot}   match: {seek['match']}   MISMATCH: {seek['mismatch']}")
    htot = hdl["match"] + hdl["mismatch"]
    print(f"\n=== HANDLER idx10 (0x7665) SHADOW (full state machine; obj+def writes) ===")
    print(f"  checks: {htot}   match: {hdl['match']}   MISMATCH: {hdl['mismatch']}")
    for m in hdl_mismatches:
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
