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
    demo = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_full_gorilla_20260628_203423"
    max_ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 100_000
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
        rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                                game_root=str(ROOT / "assets"), native_replacements=True)
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

        def advance() -> bool:                   # drive the VM one present-frame with the demo's input (SB on)
            if pb.finished(frame[0]):
                return False
            pb.apply_to_runtime(frame[0], rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
            play._advance_demo_frame(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                     sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
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
