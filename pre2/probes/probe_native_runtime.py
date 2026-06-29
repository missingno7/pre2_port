"""Verify the VM-less runtime loop runs and animates.

Boot a NativeGameState from a gameplay snapshot, then step+render N frames with NO VM (native_frame_step:
native_gameplay_frame -> native_render). Asserts it runs without error AND that the rendered frame keeps
changing — i.e. it is a live simulation (objects/player/projectiles advancing), not a frozen image. This is the
standalone seam end to end: NativeGameState in, displayed pixels out, no emulator.

Run: python -m pre2.probes.probe_native_runtime
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot
from pre2.native.state import NativeGameState
from pre2.native.runtime import native_frame_step
from sdl_view import render_planar_rgb_from_planes
import play

_TIMER = 0x27EE
_DS_BASE = 0x1A0F << 4


def main():
    pb = InputDemoPlayback.load(str(ROOT / "artifacts/demo_pre2_20260629_141422"))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}
    for f in range(90):                                       # warm up to active gameplay
        pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)

    # --- VM-less standalone loop: boot once from the snapshot, then step+render with no VM ---
    state = NativeGameState(bytearray(cpu.mem.data))
    disp = rt.program.memory.ega_display_start
    gr = str(ROOT / "assets")
    prev = None
    changed = 0
    n = 30
    for _ in range(n):
        planes, page = native_frame_step(state, rt.dos, disp, game_root=gr)
        rgb = np.asarray(render_planar_rgb_from_planes(planes, page, rt.dos.vga_palette)).tobytes()
        if prev is not None and rgb != prev:
            changed += 1
        prev = rgb
        state.data[_DS_BASE + _TIMER] = (state.data[_DS_BASE + _TIMER] + 1) & 0xFF   # advance the frame timer

    print(f"ran {n} VM-less frames (boot + native_frame_step x{n}); frames that changed: {changed}/{n - 1}")
    ok = changed >= 10                                        # a live simulation, not a frozen frame
    print("native runtime loop:", "PASS (runs + animates VM-less)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
