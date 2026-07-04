"""Prove the VM-less native core reproduces the VM, tick for tick, over a whole recording.

This is the mode-independent verification the game-tick demo enables (see pre2/native/game_tick_demo.py):

  1. RECORD a tick timeline by driving the VM with an existing input demo — per game tick (one main-loop
     iteration) capture the keys the game samples + a digest of the gameplay state after the tick. (The VM is
     the oracle; the SB is off so audio stays static — it is gameplay-irrelevant and excluded from the digest.)
  2. VERIFY: replay that timeline on the VM-less native core (native_gameplay_frame == one tick, injecting the
     recorded keys) and assert native's gameplay digest equals the recording's at EVERY tick.

All ticks match ⟹ the native VM-less game computed byte-identical gameplay to the VM across the whole demo.
Because the demo is keyed to game ticks (not the instruction-count clock), it is reproducible in every mode.

    python scripts/verify_native_tick_demo.py [demo_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.native.game_tick_demo import record_from_vm, verify_native
from pre2.runtime import load_pre2_snapshot
import play


def main() -> int:
    # ORACLE MODE: the tick timeline must be recorded with the SAME replacement mode the demo was recorded in,
    # else the instruction-per-tick count (which the demo's present-frame clock rides on) desyncs and the
    # playthrough diverges — the recording then TRUNCATES the moment the desynced player dies/gets stuck (that
    # produced the short/stale tick files that made a later `--play-demo` stop before the interesting event).
    # AUTO-SELECT from the demo's `replacements` metadata (pure|safe|hybrid); ``--hybrid`` / ``--pure`` /
    # ``--safe`` force it (needed for old demos without the key).
    force_hybrid = "--hybrid" in sys.argv
    force_pure = "--pure" in sys.argv or "--no-replacements" in sys.argv
    force_safe = "--safe" in sys.argv or "--safe-hooks" in sys.argv
    argv = [a for a in sys.argv if a not in ("--hybrid", "--pure", "--no-replacements", "--safe", "--safe-hooks")]
    demo = argv[1] if len(argv) > 1 else "artifacts/demo_pre2_full_gorilla_20260628_203423"
    max_ticks = int(argv[2]) if len(argv) > 2 else 100_000
    tick_file = ROOT / demo / "game_tick_demo.bin"
    if tick_file.exists():
        from pre2.native.game_tick_demo import GameTickDemo
        gtd = GameTickDemo.load(tick_file)
        print(f"loaded the recorded tick timeline {tick_file.name} ({gtd.n_ticks} ticks) — VM-free re-verify")
    else:
        from dos_re.runtime import enable_sound_blaster
        pb = InputDemoPlayback.load(str(ROOT / demo))
        meta = pb.manifest.get("metadata", {})
        chunk = int(meta.get("chunk_steps", 2142))          # the demo's OWN clock (old demos use 625/240 —
        hz = int(meta.get("present_hz", 70))                # hardcoding 2142 corrupts their trajectory)
        # The recorded execution mode: prefer the explicit `replacements` metadata key (stamped by play.py's
        # recorder). Old demos lack it — fall back to sniffing command_tail, which never actually contains
        # play.py flags (it is the DOS program's own tail), i.e. old pure-ASM demos auto-select WRONG; force
        # with --pure / --safe / --hybrid for those.
        if "replacements" in meta:
            recorded_mode = str(meta["replacements"])
            tail = f"replacements={recorded_mode}"
        else:
            tail = str(meta.get("command_tail", ""))
            recorded_mode = "pure" if ("--no-replacements" in tail or "--norepl" in tail) else "hybrid"
        auto = not (force_hybrid or force_pure or force_safe)
        mode = ("hybrid" if force_hybrid else "pure" if force_pure else "safe" if force_safe
                else recorded_mode)                                  # match how the demo was recorded
        # Recording HYBRID proves native == the hybrid VM (same recovered logic native runs); PURE ASM proves
        # native == the original PRE2.EXE; SAFE proves native == original GAME LOGIC (only render/audio-owned
        # hooks were live, and those cannot write gameplay-owned state — pre2.checkpoints.SAFE_ORACLE_HOOKS).
        label = {"hybrid": "HYBRID VM (recovered hooks)", "pure": "PURE ASM (original PRE2.EXE)",
                 "safe": "SAFE HOOKS (original game logic, render/audio hooks only)"}[mode]
        print(f"  oracle = {label}{f' [auto from {tail!r}]' if auto else ''}")
        rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                                game_root=str(ROOT / "assets"), native_replacements=mode)
        cpu = rt.cpu
        cpu.trace_enabled = False
        det = lambda: cpu.instruction_count / (chunk * hz)
        rt.dos.time_source = det
        # CRITICAL: replay with the Sound Blaster ENABLED, exactly as play.py's _run_replay_headless does. The
        # SB ISR/DMA contributes to the per-frame INSTRUCTION budget the demo clock runs on; recording the tick
        # timeline WITHOUT it shifts where every input lands (the demo desyncs — player fell into a pit instead
        # of playing). Audio is dropped (no sink) + excluded from the gameplay digest; only its timing matters.
        sb = enable_sound_blaster(rt)
        sb.clock = det
        tick = {"next": 0.0}
        frame = [0]

        # Drive with the recovered wait fast-forward, NOT the interpreted stepper: byte-equivalent (proven by
        # tests/test_timing_fastforward.py + the fast-vs-interpreted whole-memory probes), and mandatory at
        # the safe-recording chunk budget (150k steps/frame is ~95% retrace/PIT spin — interpreting it made
        # this recorder grind for ~15 min on a 2-minute demo; the fast path collapses it ~30x).
        from pre2.bridge.timing_fastforward import advance_frame_fast

        def advance() -> bool:                   # drive the VM one present-frame with the demo's input (SB on)
            if pb.finished(frame[0]):
                return False
            pb.apply_to_runtime(frame[0], rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
            advance_frame_fast(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                               sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick,
                               det_speed=chunk * hz, active_fraction=rt.dos.vga_retrace_active_fraction,
                               base=0.0)
            if sb.pcm_out:
                sb.pcm_out.clear()
            frame[0] += 1
            return True

        print(f"recording game-tick timeline from {demo.split('/')[-1]} (VM oracle, cap {max_ticks} ticks) ...")
        gtd = record_from_vm(rt, advance_one_frame=advance, max_ticks=max_ticks)
        print(f"  captured {gtd.n_ticks} game ticks (seed {len(gtd.seed):,} bytes)")
        if gtd.n_ticks:
            gtd.save(tick_file)
            print(f"  saved {tick_file.name} — play_native --play-demo replays it DETERMINISTICALLY from now on")

    print("verifying the VM-less native core against the timeline ...")
    n_ok, div = verify_native(gtd, game_root=str(ROOT / "assets"))
    if div is None:
        print(f"  PASS: native reproduced ALL {n_ok} ticks byte-identically (gameplay digest matched every tick)")
        return 0
    if div.startswith("LEVEL-END") or div.startswith("GAME-OVER"):
        print(f"  native reproduced ALL {n_ok} gameplay ticks byte-identically, then {div}")
        return 0
    print(f"  DIVERGED: native matched {n_ok}/{gtd.n_ticks} ticks, then {div}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
