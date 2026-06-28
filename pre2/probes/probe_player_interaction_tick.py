"""Whole-pass shadow of the COMPOSED player_interaction_tick (1030:8295) — the exact function the live-hook
will run (gate + loop1 + loop2 over ONE overlay), vs the ASM's whole 8295->ret. The sibling
probe_player_interaction verifies loop1 and loop2 separately; this verifies the compose (loop1's writes
threaded into loop2's reads) as a single unit. Replay each demo in the mode it was RECORDED in (see
probe_player_interaction / [[pre2-demo-clock]]); 8295 is never live-hooked so its ASM is the oracle.

Run: python -m pre2.probes.probe_player_interaction_tick
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
import pre2.recovered.player_interaction as pi
import pre2.recovered.object_inject as oi
import play

CS = 0x1030
TICK = (CS, 0x8295)
# the whole routine's RET sites: loop1's early returns (stomp/hurt/death) + loop2's exits
RETS = (0x833F, 0x8389, 0x83CD, 0x8617, 0x8858, 0x885E, 0x8829, 0x8509)
# Out-of-contract side effects the long-routine window can capture but that are NOT player_interaction's
# game-state contract: play_sfx digital-sound state ([0x1004-7], [0x27F0]) + ISR/sound state ([0x282d],
# [0x2874]) — none is a game-state offset (player/objects/entities/score live in 0x4Fxx/0x50xx/0x6Cxx), none
# is written by the recovered handlers, and they don't change across play_sfx calls. Excluded from the
# completeness check; the contract check (predicted writes match) covers all game state.
_OUT_OF_CONTRACT = frozenset((0x1004, 0x1005, 0x1006, 0x1007, 0x27F0, 0x282D, 0x2874))


def _run(demo, max_frames, native, stats, mism):
    pb = InputDemoPlayback.load(demo)
    rt = load_pre2_snapshot(str(ROOT / "assets" / "pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=native)
    cpu = rt.cpu; cpu.trace_enabled = False; md = cpu.mem.data
    pend = {}

    def base():
        return (cpu.s.ds << 4) & 0xFFFFF

    class Ov:
        def __init__(s): s.w = {}
        def rb(s, o): o &= 0xFFFF; return s.w[o] if o in s.w else md[(base() + o) & 0xFFFFF]
        def rw(s, o): return s.rb(o) | (s.rb((o + 1) & 0xFFFF) << 8)
        def apply(s, wr):
            for off, (val, wid) in wr.items():
                for k in range(wid):
                    s.w[(off + k) & 0xFFFF] = (val >> (8 * k)) & 0xFF

    def at_tick(c):
        snap = bytes(md[base():base() + 0x10000]); ov = Ov()
        read_id = lambda slot: ov.rw(0x4FD0 + slot * 0x12 + 4)
        try:
            pi.player_interaction_tick(ov.rb, ov.rw, ov.apply, lambda s: None,
                                       lambda: oi.find_free_object_slot(read_id))
            pend["w"] = (ov.w, snap); stats["tick"] += 1
        except pi.Loop2NeedsHelper as e:
            stats[f"NEEDS:{e}"] += 1
        interpret_current_instruction_without_hook(c)

    def at_ret(c):
        d = pend.pop("w", None)
        if d is not None:
            w, snap = d; post = md[base():base() + 0x10000]
            bad = next((f"PRED [{o:#06x}]={v:#04x} asm={post[o]:#04x}" for o, v in w.items() if post[o] != v), None)
            if bad:
                stats["TICK_BAD"] += 1
                if len(mism) < 30: mism.append(f"{Path(demo).name}: {bad}")
            else:
                if w: stats["tick_hit"] += 1
                # completeness: any ASM-changed byte we did not predict (excluding play_sfx sound state)
                un = [o for o in range(0x10000) if post[o] != snap[o] and w.get(o) != post[o] and o not in _OUT_OF_CONTRACT]
                if un:
                    stats["UNMODELED"] += len(un)
                    for o in un[:12]:
                        if len(mism) < 40: mism.append(f"{Path(demo).name}: UNMODELED [{o:#06x}] {snap[o]:#04x}->{post[o]:#04x}")
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[TICK] = at_tick; cpu.hook_names[TICK] = "tick"
    for ip in RETS:
        cpu.replacement_hooks[(CS, ip)] = at_ret; cpu.hook_names[(CS, ip)] = "ret"

    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < max_frames:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        frame += 1
    print(f"  {Path(demo).name}: {frame} frames")


def main():
    demos = [("artifacts/demo_pre2_20260627_213332", 600, False),
             ("artifacts/demo_pre2_20260627_190542", 600, False),
             ("artifacts/demo_pre2_20260626_115215", 600, False),
             ("artifacts/demo_pre2_20260626_140619", 600, False),
             ("artifacts/demo_pre2_20260628_142652", 2000, False),  # bomb
             ("artifacts/demo_pre2_20260628_151845", 500, True),    # grenade (hybrid)
             ("artifacts/demo_pre2_20260628_152002", 300, True)]    # extra-life (hybrid)
    stats = Counter(); mism = []
    for d, mf, native in demos:
        _run(d, mf, native, stats, mism)
    print("\n=== player_interaction_tick whole-pass shadow ===")
    for k in sorted(stats):
        print(f"  {k:14s} {stats[k]}")
    for m in mism:
        print("  " + m)
    ok = stats["TICK_BAD"] == 0 and stats["UNMODELED"] == 0
    print("\nWHOLE-PASS (8295 player_interaction_tick):", "PASS (byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
