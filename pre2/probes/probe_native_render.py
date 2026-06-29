"""Verify native_render produces a real gameplay frame from a NativeGameState.

The recovered faithful renderer reconstructs the frame from the high-level game state (camera/tiles/objects/
palette/HUD), reading only ``mem.data`` — so it is VM-object-independent. This probe renders one gameplay frame
twice from the SAME data, once via the live VM ``mem`` and once via a NativeGameState built from ``mem.data``,
and asserts the planes are pixel-identical. That proves the render layer consumes native game state directly
(the seam between the recovered gameplay sim and the recovered renderer), with only the VGA palette/page passed
alongside (the piece a standalone runtime owns instead of the emulated VGA).

Run: python -m pre2.probes.probe_native_render
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot
from pre2.native.state import NativeGameState
from pre2.native.render import native_render
import play


def main():
    demo = "artifacts/demo_pre2_20260629_141422"
    pb = InputDemoPlayback.load(str(ROOT / demo))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}
    for frame in range(60):                                  # advance to a stable gameplay frame
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)

    dos = rt.dos
    disp = rt.program.memory.ega_display_start
    gr = str(ROOT / "assets")
    ref_planes, ref_page = native_render(cpu.mem, dos, disp, game_root=gr)
    native = NativeGameState(bytearray(cpu.mem.data))
    nat_planes, nat_page = native_render(native, dos, disp, game_root=gr)

    same = ref_page == nat_page and all(bytes(ref_planes[p]) == bytes(nat_planes[p]) for p in range(4))
    nonblank = any(b for p in range(4) for b in nat_planes[p])
    print(f"native_render from NativeGameState: page={nat_page}, nonblank={bool(nonblank)}, "
          f"pixel-identical to VM-mem render={same}")
    ok = same and nonblank
    print("native_render:", "PASS (renders the frame from native state)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
