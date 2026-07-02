"""Gameplay PIXEL-exactness oracle: per frame, seed a NativeGameState at the loop top (021A) from the VM, inject
the demo's input, run the FULL native_gameplay_frame + native_render, and diff the rendered RGB against the VM's
committed page (1030:6772). This is exactly what play_native does per frame; re-seeding each frame isolates the
renderer + one-frame gameplay from long-run state drift, so a nonzero diff is a real render/one-frame gap.

Pixel-exactness is the ultimate proof the standalone behaves like the original DURING gameplay. Reports, per
demo: how many rendered frames are pixel-identical, the first divergence (frame + region + saved PNGs), and a
right-edge-vs-interior split.

    python -m pre2.probes.probe_native_render_pixel [demo_substr] [max_frames]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback                       # noqa: E402
from dos_re.interrupts import deliver_scancode                        # noqa: E402
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE              # noqa: E402
from pre2.checkpoints.common import Pre2CaveTeleport                  # noqa: E402
from pre2.native.loop import native_cave_teleport, native_gameplay_frame  # noqa: E402
from pre2.native.render import native_render, native_sync_render_state  # noqa: E402
from pre2.native.state import NativeGameState                         # noqa: E402
from pre2.probes.probe_native_frame import KBD                        # noqa: E402
from pre2.runtime import load_pre2_snapshot                           # noqa: E402
from sdl_view import render_planar_rgb_from_planes                    # noqa: E402
import play                                                           # noqa: E402

DS = 0x1A0F
DS_BASE = DS << 4
FRAME_TOP, DECODE, COMMIT = 0x021A, 0x0DC1, 0x6772


def _run(demo: str, max_frames: int, out_dir: Path):
    gr = str(ROOT / "assets")
    pb = InputDemoPlayback.load(str(ROOT / demo))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=gr, native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    ns = {"seed": None, "kbd": None, "n": 0, "zero": 0, "first": None, "worst": 0, "diffs": [], "done": False}
    orig = cpu.step

    def vm_planes(disp):
        return [bytes(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE:
                               EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000]) for p in range(4)]

    def sstep():
        s = cpu.s
        if not ns["done"] and (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
            ip = s.ip & 0xFFFF
            if ip == FRAME_TOP:
                ns["seed"] = bytearray(mem.data); ns["kbd"] = None
            elif ip == DECODE and ns["seed"] is not None:
                ns["kbd"] = {o: mem.data[DS_BASE + o] for o in KBD}
            elif ip == COMMIT and ns["seed"] is not None and ns["kbd"] is not None:
                disp = rt.program.memory.ega_display_start; pal = list(rt.dos.vga_palette)
                vm_rgb = np.asarray(render_planar_rgb_from_planes(vm_planes(disp), disp, pal), np.uint8)
                st = NativeGameState(bytearray(ns["seed"]))
                for o, v in ns["kbd"].items():
                    st.data[DS_BASE + o] = v
                try:
                    try:
                        native_gameplay_frame(st)
                    except Pre2CaveTeleport as tp:                    # drain the transition (state-only)
                        for _ in native_cave_teleport(st, tp.si):
                            pass
                    native_sync_render_state(st)
                    planes, page = native_render(st, rt.dos, disp, game_root=gr)
                except Exception as e:                                # noqa: BLE001
                    print(f"  frame {ns['n']}: native raised {type(e).__name__}: {str(e)[:70]}")
                    ns["done"] = True; orig(); return
                n_rgb = np.asarray(render_planar_rgb_from_planes(planes, page, pal), np.uint8)
                d = (vm_rgb != n_rgb).any(2); ndiff = int(d.sum())
                ns["worst"] = max(ns["worst"], ndiff)
                if ndiff:
                    cols = np.where(d.any(0))[0]; rows = np.where(d.any(1))[0]
                    ns["diffs"].append((ns["n"], ndiff, int(cols.min()), int(cols.max())))
                    if ns["first"] is None:
                        ns["first"] = ns["n"]
                        print(f"  FIRST diff @frame {ns['n']}: {ndiff} px; rows {rows.min()}..{rows.max()} "
                              f"cols {cols.min()}..{cols.max()} (game={int(d[:176].sum())} hud={int(d[176:].sum())})")
                        try:
                            from PIL import Image
                            for tag, rgb in (("vm", vm_rgb), ("native", n_rgb)):
                                Image.fromarray(rgb).resize((640, 400), 0).save(out_dir / f"pixdiff_{tag}_f{ns['n']}.png")
                        except Exception:                            # noqa: BLE001
                            pass
                else:
                    ns["zero"] += 1
                ns["seed"] = None; ns["kbd"] = None; ns["n"] += 1
                if ns["n"] >= max_frames:
                    ns["done"] = True
        orig()

    cpu.step = sstep
    det = lambda: cpu.instruction_count / (2142 * 70)                 # noqa: E731
    rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and not ns["done"] and frame < 4000:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        frame += 1
    edge = [x for x in ns["diffs"] if x[2] >= 280]
    print(f"  -> {Path(demo).name}: {ns['n']} frames; PIXEL-EXACT={ns['zero']}, diff={len(ns['diffs'])}, "
          f"worst={ns['worst']} px (right-edge {len(edge)} / interior {len(ns['diffs']) - len(edge)})")
    return ns["zero"], ns["n"]


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else "gorilla"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    demos = [d for d in ["artifacts/demo_pre2_full_gorilla_20260628_203423",
                         "artifacts/demo_pre2_20260629_141422"] if want in d or want == "all"]
    if not demos:
        demos = [f"artifacts/{want}"]
    out_dir = ROOT / "artifacts"
    for d in demos:
        print(f"\n{Path(d).name}:")
        _run(d, lim, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
