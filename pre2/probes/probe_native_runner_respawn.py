"""Headless check of the STANDALONE RUNNER's user-visible respawn BEHAVIOR — the thing the byte-exact state
verifies do not cover, and the thing that was actually broken (instant respawn, no animation).

It bootstraps exactly like scripts/play_native.py (snapshot -> native_level_init), arms a death ([0x6be4]=2,
as the boss hit leaves it), then drives ``native_frame_step`` like the runner does and asserts it YIELDS the whole
multi-frame death-bounce (~61 frames: 60 bounce + the checkpoint) rather than a single instant-respawn frame —
and that the player visibly arcs (Y rises then falls) before snapping to the checkpoint. This is the regression
guard for "you respawn immediately, before the death animation plays".

    python -m pre2.probes.probe_native_runner_respawn
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback                 # noqa: E402
from pre2.native.level_init import native_level_init            # noqa: E402
from pre2.native.runtime import native_frame_step               # noqa: E402
from pre2.native.state import NativeGameState                   # noqa: E402
from pre2.runtime import load_pre2_snapshot                     # noqa: E402
import play                                                     # noqa: E402

DS = 0x1A0F << 4
_SNAP = "artifacts/demo_pre2_full_gorilla_20260628_203423"


def _rw(d, o): return d[DS + o] | (d[DS + o + 1] << 8)


def main():
    gr = str(ROOT / "assets")
    pb = InputDemoPlayback.load(str(ROOT / _SNAP))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(), game_root=gr,
                            native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (2142 * 70); rt.dos.time_source = det; tick = {"next": 0.0}
    for _ in range(30):
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
    dos = rt.dos; disp = rt.program.memory.ega_display_start

    state = NativeGameState(bytearray(cpu.mem.data))
    native_level_init(state, game_root=gr)

    # --- a NORMAL frame yields exactly one displayed frame ---
    normal = list(native_frame_step(state, dos, disp, game_root=gr))
    print(f"normal frame -> {len(normal)} displayed frame(s)")

    # --- arm the respawn the way the boss hit does ([0x6be4]=2; the player step counts 2->1, then 4C69 fires
    #     4F6C) and drive the runner step, capturing the player Y at each displayed frame ---
    state.data[DS + 0x6BE4] = 2
    ys: list[int] = []
    for planes, page in native_frame_step(state, dos, disp, game_root=gr):
        ys.append(_rw(state.data, 0x4F1E))                         # player Y at each rendered bounce frame
    n = len(ys)
    y0, ymin, ymax, ylast = ys[0], min(ys), max(ys), ys[-1]
    print(f"respawn frame -> {n} displayed frames; player Y: start={y0} min={ymin} max={ymax} checkpoint={ylast}")

    # the bounce must ANIMATE (many frames, with the corpse rising — Y decreasing — before falling), not teleport
    animated = n >= 0x3C
    arced = (y0 - ymin) >= 0x20                                    # the corpse visibly jumps up before falling
    restored = (ymax - ylast) >= 0x100                            # the LAST frame snaps back from the fall to the
    #                                                               checkpoint (here = the level start) — not left
    #                                                               where it fell through (ymax)
    ok = len(normal) == 1 and animated and arced and restored
    print("\nrunner respawn behavior:",
          "PASS (the death-bounce animates over ~60 frames, the corpse arcs, then snaps to the checkpoint)" if ok
          else f"FAIL (animated={animated} arced={arced} restored={restored} normal={len(normal)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
