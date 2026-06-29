"""Verify host input drives the native game, VM-less.

Boot a NativeGameState into a runnable level, then hold RIGHT (+ periodic jump) over a standalone loop and assert
the player runs right and the camera follows — the full host-input chain with no emulator:
apply_input -> the [0x27F4] scancode table -> DC1 (input decode) -> player FSM -> camera follow.

Run: python -m pre2.probes.probe_native_input
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
from pre2.native.runtime import native_frame_step
from pre2.native.input import apply_input
import play

_DS_BASE = 0x1A0F << 4
_PX, _CAM_X, _TIMER = 0x4F1C, 0x2DE4, 0x27EE


def main():
    pb = InputDemoPlayback.load(str(ROOT / "artifacts/demo_pre2_full_gorilla_20260628_203423"))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}
    for f in range(40):                                       # warm up into the level
        pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)

    # --- VM-less, host-driven: hold RIGHT, hop periodically ---
    state = NativeGameState(bytearray(cpu.mem.data))
    disp = rt.program.memory.ega_display_start
    gr = str(ROOT / "assets")
    x0, cam0 = state.rw(_PX), state.rw(_CAM_X)
    for i in range(48):
        apply_input(state, right=True, up=(i % 18 < 3))
        native_frame_step(state, rt.dos, disp, game_root=gr)
        state.data[_DS_BASE + _TIMER] = (state.data[_DS_BASE + _TIMER] + 1) & 0xFF
    x1, cam1 = state.rw(_PX), state.rw(_CAM_X)
    dx = (x1 - x0) & 0xFFFF

    print(f"held RIGHT 48 frames VM-less: player X {x0:#06x} -> {x1:#06x} (dX={dx}); camera X {cam0} -> {cam1}")
    ok = dx > 0x80 and cam1 > cam0                            # ran right a meaningful distance + camera scrolled
    print("native input:", "PASS (host RIGHT drives player + camera VM-less)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
