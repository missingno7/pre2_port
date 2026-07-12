"""Verify a FRONT-END scene renders over a NativeGameState — the foundation for the standalone front-end.

The recovered front-end scene leaves (OLDIES / title images / mode-menu / world-map) reconstruct their frame from
the high-level game state (text layout, font, the live year), reading only ``mem.data`` — exactly like the gameplay
``native_render`` does. So they run over a NativeGameState with no VM object, and a standalone front-end is a flow
driver sequencing these leaves rather than fresh reverse-engineering.

This probe boots a fresh PRE2 to the OLDIES easter-egg screen, then renders it TWICE — once via the live VM ``mem``
and once via a NativeGameState built from ``mem.data`` — and asserts the four planes are pixel-identical (and the
year text is non-blank). That pins the seam: ``bridge.oldies_scene.build_oldies_scene`` is VM-object-independent.

Run: python -m pre2.probes.probe_native_scene_oldies
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from pre2.runtime import create_pre2_runtime
from pre2.native.state import NativeGameState, DATA_SEG
from pre2.views.oldies_scene import build_oldies_scene
import play

_DS = DATA_SEG << 4


def main() -> int:
    rt = create_pre2_runtime(str(ROOT / "assets/pre2.exe"), game_root=str(ROOT / "assets"),
                             native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}
    for _ in range(130):                                          # boot to the OLDIES screen
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)

    page = rt.program.memory.ega_display_start
    year = cpu.mem.data[_DS + 0x37] | (cpu.mem.data[_DS + 0x38] << 8)

    ref_planes, _ = build_oldies_scene(cpu.mem, page=page)                       # over the live VM mem
    native = NativeGameState(bytearray(cpu.mem.data))
    nat_planes, _ = build_oldies_scene(native, page=page)                        # over a NativeGameState

    same = all(bytes(ref_planes[p]) == bytes(nat_planes[p]) for p in range(4))
    nonblank = any(b for p in range(4) for b in nat_planes[p])
    print(f"OLDIES scene over NativeGameState (year={year}): nonblank={bool(nonblank)}, "
          f"pixel-identical to VM-mem render={same}")
    ok = same and nonblank and year >= 1996
    print("native front-end scene (OLDIES):", "PASS (renders VM-less from NativeGameState)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
