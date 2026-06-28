"""Shadow-verify the recovered player-interaction keystones (spawn_pickup_effect 8875, advance_anim_script 80CB)
byte-exact vs the ASM, on a replayed demo (pure ASM). Predict at the routine entry, step aside, compare the
predicted writes + the OWNED regions at the ret. Run: python -m pre2.probes.probe_player_interaction [demo ...]
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
import pre2.recovered.player_interaction as pi
import play

CS = 0x1030
SPAWN = (CS, 0x8875); SPAWN_RET = (CS, 0x88D6)
ANIM = (CS, 0x80CB); ANIM_RET = (CS, 0x80DD)
LOOP1 = (CS, 0x8295); LOOP1_EXITS = (0x833F, 0x8389, 0x83CD, 0x83D7)   # stomp-ret/knock-ret/death-ret/loop2
# regions the keystones own (ISR sound state is in low 0x27xx/0x28xx, outside these)
_OWNED_SPAWN = (frozenset(range(0x4FD0, 0x5800)) | {0x6C0E, 0x6C0F, 0x6C10, 0x6C11} | {0xA33E, 0xA33F})


class _Ov:
    """Read-through copy-overlay of the 64KB DS; accumulates the recovered writes."""
    def __init__(self, snap):
        self.snap = snap; self.w = {}

    def rb(self, o):
        o &= 0xFFFF
        return self.w.get(o, self.snap[o])

    def rw(self, o):
        return self.rb(o) | (self.rb((o + 1) & 0xFFFF) << 8)

    def apply(self, writes):
        for off, (val, width) in writes.items():
            for k in range(width):
                self.w[(off + k) & 0xFFFF] = (val >> (8 * k)) & 0xFF


def _run(demo, max_frames, stats, mism, native_replacements=False):
    """Replay ``demo`` and shadow-verify. ``native_replacements`` MUST match the mode the demo was RECORDED
    in: a hybrid-recorded demo (hooks on) only reproduces its trajectory with hooks on (live hooks collapse
    work, shifting the ic-based clock -> a timing-sensitive pickup lands differently in pure ASM). 8295/loop2
    is never live-hooked, so its ASM stays the oracle either way; in hybrid mode the keystone shadows (8875/
    80CB) are skipped because those ARE live (no ASM to compare)."""
    pb = InputDemoPlayback.load(demo)
    meta = pb.manifest.get("metadata", {})
    args = argparse.Namespace(exe=str(ROOT / "assets" / "pre2.exe"), game_root=str(ROOT / "assets"),
                              audio="off", steps=None, no_replacements=not native_replacements, fast_retrace_waits=False,
                              chunk_steps=int(meta.get("chunk_steps", 2142)), present_hz=int(meta.get("present_hz", 70)),
                              timer_irq=bool(meta.get("timer_irq", True)), input_irq_steps=int(meta.get("input_irq_steps", 2_000_000)))
    rt = load_pre2_snapshot(args.exe, pb.snapshot_path(), game_root=args.game_root, native_replacements=native_replacements)
    cpu = rt.cpu; cpu.trace_enabled = False; md = cpu.mem.data
    pend = {}

    def b():
        return (cpu.s.ds << 4) & 0xFFFFF

    def rb(o):
        return md[(b() + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        return md[(b() + (o & 0xFFFF)) & 0xFFFFF] | (md[(b() + ((o + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def _flat(writes):
        out = {}
        for off, (val, width) in writes.items():
            for k in range(width):
                out[(off + k) & 0xFFFF] = (val >> (8 * k)) & 0xFF
        return out

    def at_spawn(c):
        w = _flat(pi.spawn_pickup_effect(rb, rw, c.s.ax, c.s.si))
        pend["s"] = (w, bytes(md[b():b() + 0x10000]))
        stats["spawn"] += 1
        interpret_current_instruction_without_hook(c)

    def at_spawn_ret(c):
        d = pend.pop("s", None)
        if d:
            w, snap = d; post = md[b():b() + 0x10000]; bad = None
            for off, v in w.items():
                if post[off] != v:
                    bad = f"spawn PRED [{off:#06x}]={v:#04x} asm={post[off]:#04x}"; break
            if bad is None:
                for off in _OWNED_SPAWN:
                    if post[off] != snap[off] and off not in w:
                        bad = f"spawn UNMODELED [{off:#06x}] {snap[off]:#04x}->{post[off]:#04x}"; break
            if bad:
                stats["SPAWN_BAD"] += 1
                if len(mism) < 20:
                    mism.append(bad)
        interpret_current_instruction_without_hook(c)

    def at_anim(c):
        w = _flat(pi.advance_anim_script(rw, c.s.di))
        pend["a"] = (w, c.s.di)
        stats["anim"] += 1
        interpret_current_instruction_without_hook(c)

    def at_anim_ret(c):
        d = pend.pop("a", None)
        if d:
            w, di = d; post = md[b():b() + 0x10000]
            for off, v in w.items():
                if post[off] != v:
                    stats["ANIM_BAD"] += 1
                    if len(mism) < 20:
                        mism.append(f"anim PRED [{off:#06x}]={v:#04x} asm={post[off]:#04x}")
                    break
        interpret_current_instruction_without_hook(c)

    def at_loop1(c):
        snap = bytes(md[b():b() + 0x10000])
        ov = _Ov(snap)
        pi.loop1(ov.rb, ov.rw, ov.apply, lambda s: None)        # predict the whole loop1 walk
        pend["L"] = ov.w
        stats["loop1"] += 1
        interpret_current_instruction_without_hook(c)

    def at_loop1_exit(c):
        w = pend.pop("L", None)
        if w is not None:
            post = md[b():b() + 0x10000]
            for off, v in w.items():                            # predicted game-state writes must match ASM
                if post[off] != v:
                    stats["LOOP1_BAD"] += 1
                    if len(mism) < 20:
                        mism.append(f"loop1 PRED [{off:#06x}]={v:#04x} asm={post[off]:#04x}")
                    break
            else:
                if w:
                    stats["loop1_hit"] += 1                      # a tick that actually wrote something
        interpret_current_instruction_without_hook(c)

    # --- loop2 shadow (predict the whole pickup walk at 83D7, compare predicted writes at the exits) ---
    LOOP2 = (CS, 0x83D7); LOOP2_EXITS = (0x8617, 0x8858, 0x885E, 0x8829, 0x8509)

    def at_loop2(c):
        snap = bytes(md[b():b() + 0x10000]); ov = _Ov(snap)
        es = ov.rw(0x2DDA); eb = (es << 4) & 0xFFFFF
        read_id = lambda s: ov.rw(0x4FD0 + s * 0x12 + 4)
        try:
            pi.loop2(ov.rb, ov.rw, ov.apply, lambda s: None,
                     lambda: __import__("pre2.recovered.object_inject", fromlist=["find_free_object_slot"]).find_free_object_slot(read_id))
            pend["2"] = ov.w; stats["loop2"] += 1
        except pi.Loop2NeedsHelper as e:
            stats[f"NEEDS:{e}"] += 1
        interpret_current_instruction_without_hook(c)

    def at_loop2_exit(c):
        w = pend.pop("2", None)
        if w is not None:
            post = md[b():b() + 0x10000]
            for off, v in w.items():
                if post[off] != v:
                    stats["LOOP2_BAD"] += 1
                    if len(mism) < 20:
                        mism.append(f"loop2 PRED [{off:#06x}]={v:#04x} asm={post[off]:#04x}")
                    break
            else:
                if w:
                    stats["loop2_hit"] += 1
        interpret_current_instruction_without_hook(c)

    # loop1 is verified separately (its 0x83D7 exit == loop2 entry); here focus on loop2 + the keystones.
    hooks = [(LOOP2, at_loop2, "L2")]
    if not native_replacements:                                  # keystones are live in hybrid -> no ASM oracle
        hooks += [(SPAWN, at_spawn, "s0"), (SPAWN_RET, at_spawn_ret, "s1"),
                  (ANIM, at_anim, "a0"), (ANIM_RET, at_anim_ret, "a1")]
    for key, fn, nm in hooks:
        cpu.replacement_hooks[key] = fn; cpu.hook_names[key] = nm
    for ip in LOOP2_EXITS:
        cpu.replacement_hooks[(CS, ip)] = at_loop2_exit; cpu.hook_names[(CS, ip)] = "L2e"

    det_speed = max(1, args.chunk_steps * args.present_hz)
    det_now = lambda: cpu.instruction_count / det_speed
    rt.dos.time_source = det_now; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < max_frames:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=args.input_irq_steps))
        play._advance_demo_frame(rt, chunk_steps=args.chunk_steps, sub_batch=2000, clock=det_now, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps, tick_state=tick)
        frame += 1
    print(f"  {Path(demo).name}: {frame} frames")


def main():
    # (demo, native_replacements, max_frames). native MUST match the recording mode (see _run).
    if sys.argv[1:]:
        demos = [(d, False, 600) for d in sys.argv[1:]]
    else:
        demos = [("artifacts/demo_pre2_20260627_213332", False, 600),
                 ("artifacts/demo_pre2_20260627_190542", False, 600),
                 ("artifacts/demo_pre2_20260626_115215", False, 600),
                 ("artifacts/demo_pre2_20260626_140619", False, 600),
                 ("artifacts/demo_pre2_20260628_142652", False, 2000),   # bomb 870A (pure-ASM reproduces)
                 ("artifacts/demo_pre2_20260628_151845", True, 500),     # grenade 86B7 (hybrid; long, 100 hits)
                 ("artifacts/demo_pre2_20260628_152002", True, 300)]     # extra-life 87E6 (hybrid)
    stats = Counter(); mism = []
    for d, native, mf in demos:
        _run(d, mf, stats, mism, native_replacements=native)
    print("\n=== player-interaction keystones shadow ===")
    for k in sorted(stats):
        print(f"  {k:12s} {stats[k]}")
    for m in mism:
        print("  " + m)
    ok = stats["SPAWN_BAD"] == 0 and stats["ANIM_BAD"] == 0 and stats["LOOP2_BAD"] == 0
    print("\nPLAYER-INTERACTION (keystones + loop2):", "PASS (byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
