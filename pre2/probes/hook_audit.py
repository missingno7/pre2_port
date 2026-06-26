"""Print the runtime hook audit over a gameplay snapshot/demo (which hooks are installed + actually firing).

    python pre2/probes/hook_audit.py [snapshot_or_demo_dir] [frames]

Loads the state, runs the hybrid runtime for N frames counting every hook's fires, and prints the audit table
(`pre2.checkpoints.audit`). A gameplay snapshot is the right witness — it exercises the per-frame gameplay loop
so the gameplay hooks (object_tick, object_render, second_pass_project_entity, frame_*, …) fire.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from pre2.checkpoints.audit import build_hook_audit, format_audit
from pre2.runtime import load_pre2_snapshot
from play import _advance_frame_deterministic


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "artifacts/snapshot_pre2_20260626_154531"
    frames = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    rt = load_pre2_snapshot(Path("assets/pre2.exe"), Path(src), game_root=Path("assets"))
    args = argparse.Namespace(chunk_steps=2142, present_hz=70, timer_irq=True,
                              input_irq_steps=2_000_000, retrace_pulse=0.06)
    det_speed = max(1, args.chunk_steps * args.present_hz)
    rt.dos.time_source = lambda: rt.cpu.instruction_count / det_speed
    ts = {"next": 0.0}

    def advance(rt, _frame):
        _advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000,
                                     clock=rt.dos.time_source, pic=rt.dos.pic, sound_blaster=None,
                                     timer_irq=True, input_irq_steps=args.input_irq_steps,
                                     tick_state=ts, det_speed=det_speed)

    rows = build_hook_audit(rt, frames=frames, advance=advance)
    print(f"source: {src}  frames: {frames}")
    print(format_audit(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
