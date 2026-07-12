"""GROUND TRUTH: capture the reference VM's front-end SCREEN TIMELINE while replaying a demo.

The front end (intro / title / menu / attract / world-map / tally) runs with NO gameplay tick, so the tick-demo
verifier captures none of it. This probe records, at every present-frame boundary, WHICH screen the VM shows (a
coarse logical id — the 13h title/menu/wall images are named) + a pixel digest. That timeline is the oracle the
VM-less native front end must reproduce (see verify_native_frontend.py).

    python scripts/probe_frontend_timeline.py <demo_dir> [--frames N] [--raw] [--save out.json]

``--raw`` prints every frame (not just screen changes). It would have caught the "expert-eater wall shown AFTER the
carte + level load instead of before" bug at a glance: the VM timeline is  map -> CASTLE -> menu, with no carte.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from dos_re.frontend_timeline import capture, collapse, format_sequence, rgb_sha
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from frontend_capture import _fingerprint_map, classify_vm_frame
from pre2.bridge.timing_fastforward import advance_frame_fast
from pre2.runtime import load_pre2_snapshot


def capture_vm_timeline(demo_dir: str, max_frames: int):
    """Replay ``demo_dir`` through the VM present-frame by present-frame, returning its front-end timeline."""
    pb = InputDemoPlayback.load(demo_dir)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 2142))
    hz = int(meta.get("present_hz", 70))
    mode = str(meta.get("replacements", "hybrid"))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=mode)
    cpu = rt.cpu
    cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (chunk * hz)                # noqa: E731
    rt.dos.time_source = det
    sb = enable_sound_blaster(rt)
    sb.clock = det
    tick = {"next": 0.0}
    fpmap = _fingerprint_map(str(ROOT / "assets"))

    def sample(i):
        if pb.finished(i):
            return None
        pb.apply_to_runtime(i, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        advance_frame_fast(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                           sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick,
                           det_speed=chunk * hz, active_fraction=rt.dos.vga_retrace_active_fraction, base=0.0)
        if sb.pcm_out:
            sb.pcm_out.clear()
        screen, rgb = classify_vm_frame(rt, str(ROOT / "assets"), fpmap)
        return screen, rgb_sha(rgb)

    print(f"replaying {Path(demo_dir).name} through the VM ({mode}); classifying each present-frame ...")
    return capture(sample, max_frames)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("demo")
    ap.add_argument("--frames", type=int, default=1200)
    ap.add_argument("--raw", action="store_true", help="print EVERY frame, not just screen changes")
    ap.add_argument("--save", metavar="FILE", help="write the timeline (screen + rgb_sha per frame) as JSON")
    args = ap.parse_args(argv)

    demo_dir = str(ROOT / args.demo) if not Path(args.demo).is_absolute() else args.demo
    records = capture_vm_timeline(demo_dir, args.frames)
    print(f"captured {len(records)} present-frames")
    if args.raw:
        for r in records:
            print(f"  f={r.frame:5d}  {r.screen:16s}  rgb={r.rgb_sha[:12]}")
    else:
        for run in collapse(records):
            print(f"  f={run.start:5d}  {run.screen:16s}  x{run.count} frames")
    print("SEQUENCE:", format_sequence(collapse(records)))
    if args.save:
        out = Path(args.save)
        out.write_text(json.dumps([[r.frame, r.screen, r.rgb_sha] for r in records]), encoding="utf-8")
        print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
