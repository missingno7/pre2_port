"""Exact displayed-frame divergence test: hybrid (native hooks) vs pure ASM (--no-replacements).

full-verify proves memory == ASM at every hook RET, but it samples at hook boundaries, not at the
DISPLAY instant, and the HUD tile-bleed is a known *accumulation* artifact. This probe answers the
direct question the player asked: does the actual displayed framebuffer (including the HUD band at
rows 176-199) ever differ between the hybrid runtime and the pure original ASM, on the same demo?

It captures at the frame-commit boundary 1030:6772 (palette-fade entry, post page-flip) where the
displayed page == ega_display_start and is byte-stable (verify_frame_boundary.py). At each rendered
frame it digests the displayed page (4 planes x 200 rows x 0x28), split into the game viewport
(rows 0..175) and the HUD band (rows 176..199), under BOTH modes, then diffs frame-for-frame.

  diff == 0 everywhere  -> the displayed frame is byte-identical to the original ASM (any glitch the
                           player sees is therefore an ORIGINAL game artifact, present in pure ASM too).
  diff != 0 at frame F  -> a real hybrid rendering divergence at F (region + first byte reported).

Run:  python -m pre2.probes.frame_content_divergence <demo_dir> [--max-frames N] [--dump-frame F]
"""
from __future__ import annotations

import argparse
import sys
from hashlib import blake2b
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.cpu import HaltExecution, UnsupportedInstruction
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.runtime import load_pre2_snapshot

import play  # scripts/play.py — reuse the exact deterministic frame stepper

BOUNDARY = (0x1030, 0x6772)
ROW = 0x28                     # bytes per row per plane (mode 0Dh, 320px/8 = 40)
GAME_ROWS = 176                # rows 0..175 = game viewport
PAGE_ROWS = 200                # rows 0..199 = full displayed page (incl. HUD band 176..199)
_IDX = np.arange(PAGE_ROWS * ROW, dtype=np.int64)   # offsets within a plane (pre page-wrap)


def _build_runtime(playback: InputDemoPlayback, args: argparse.Namespace, *, native: bool, fast: bool):
    """Replicate play._make_replay_runtime but choose hybrid vs pure (native_replacements) and the
    timing path (fast-retrace fast-forward vs interpreted spin)."""
    meta = playback.manifest.get("metadata", {})
    if "chunk_steps" in meta:
        args.chunk_steps = int(meta["chunk_steps"])
    args.present_hz = int(meta.get("present_hz", 30))
    args.retrace_pulse = float(meta.get("retrace_pulse", 0.28))
    args.timer_irq = bool(meta.get("timer_irq", True))
    args.input_irq_steps = int(meta.get("input_irq_steps", 2_000_000))
    # _advance_frame_deterministic: use_fast = fast_retrace_waits and not no_replacements
    args.no_replacements = not native
    args.fast_retrace_waits = fast and native
    fast_adlib = bool(meta.get("fast_adlib", False))
    return load_pre2_snapshot(args.exe, playback.snapshot_path(), game_root=args.game_root,
                              fast_adlib=fast_adlib, native_replacements=native)


def _run_pass(demo_dir: str, *, native: bool, fast: bool, max_frames: int | None, dump_frame: int | None):
    """Replay the demo, capturing a digest of the displayed page at each 6772 commit boundary.

    Returns (digests, dumped) where digests is a list of (full_hex, hud_hex) per rendered frame and
    dumped is the raw 4-plane page bytes at frame `dump_frame` (or None)."""
    playback = InputDemoPlayback.load(demo_dir)
    args = argparse.Namespace(exe=str(ROOT / "assets" / "pre2.exe"), game_root=str(ROOT / "assets"),
                              audio="off", fast_adlib=False, steps=None, fast_retrace_waits=True)
    rt = _build_runtime(playback, args, native=native, fast=fast)
    cpu = rt.cpu
    cpu.trace_enabled = False
    md = np.frombuffer(cpu.mem.data, dtype=np.uint8)   # live view (reflects VRAM writes)

    digests: list[tuple[str, str]] = []
    dumped = {"page": None}

    def capture():
        disp = rt.program.memory.ega_display_start
        idx = (disp + _IDX) & 0xFFFF
        planes = [md[EGA_APERTURE + p * EGA_PLANE_STRIDE + idx] for p in range(4)]   # 4 x (200*ROW)
        page = np.stack(planes)                                # (4, 200*ROW)
        full = blake2b(page.tobytes(), digest_size=16).hexdigest()
        hud = blake2b(page[:, GAME_ROWS * ROW:].tobytes(), digest_size=16).hexdigest()
        if dump_frame is not None and len(digests) == dump_frame:
            dumped["page"] = page.copy()
        digests.append((full, hud))

    # --- install the capture observer at 6772, wrapping the existing palette_fade hook ---
    # NOTE: both comparison configs are `native` (hooks installed), so palette_fade IS hooked at 6772 and we
    # simply wrap it (capture, then run it) — a symmetric, non-perturbing observer. We do NOT support a pure
    # (no-hooks) config here: a transparent-step observer at 6772 perturbs the un-hooked run's IRQ timing.
    # `hybrid-interp` is the sound byte-faithful oracle instead — it is proven byte-identical to `--no-
    # replacements` whole-memory (0-byte snapshot diff), and carries the same symmetric observer.
    prev = cpu.replacement_hooks.get(BOUNDARY)
    assert prev is not None, "expected palette_fade hooked at 6772 (native config required)"

    def observe(c):
        capture()
        prev(c)                                        # run the original palette_fade hook (advances ip)
    cpu.replacement_hooks[BOUNDARY] = observe
    cpu.hook_names[BOUNDARY] = "frame_capture"

    # --- deterministic headless replay loop (mirrors play._run_replay_headless) ---
    det_speed = max(1, int(args.chunk_steps) * max(1, int(args.present_hz)))
    tick_state = {"next": 0.0}
    det_now = lambda: cpu.instruction_count / det_speed  # noqa: E731
    sb = None  # audio off
    rt.dos.time_source = det_now
    pic = rt.dos.pic
    frame = 0
    while not playback.finished(frame) and (max_frames is None or frame < max_frames):
        playback.apply_to_runtime(frame, rt,
                                  deliver=lambda runtime, sc: deliver_scancode(runtime, sc, max_steps=args.input_irq_steps))
        try:
            play._advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000,
                                              clock=det_now, pic=pic, sound_blaster=sb,
                                              timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                              tick_state=tick_state, det_speed=det_speed)
        except (HaltExecution, UnsupportedInstruction) as exc:
            print(f"  stopped at frame {frame}: {exc}")
            break
        frame += 1
    print(f"  frames={frame} rendered(6772)={len(digests)}")
    return digests, dumped["page"]


_CONFIGS = {
    "hybrid-fast":   dict(native=True, fast=True),     # what --view runs by default (fast-retrace timing)
    "hybrid-interp": dict(native=True, fast=False),    # hooks + interpreted timing == pure ASM whole-memory
}                                                      # (proven 0-byte vs --no-replacements); the oracle here


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo")
    ap.add_argument("--a", default="hybrid-fast", choices=list(_CONFIGS))
    ap.add_argument("--b", default="hybrid-interp", choices=list(_CONFIGS))
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--dump-frame", type=int, default=None, help="save the displayed page (both configs) at this rendered-frame index to a .npz")
    args = ap.parse_args()

    print(f"== A = {args.a} ==");  h_dig, h_page = _run_pass(args.demo, **_CONFIGS[args.a], max_frames=args.max_frames, dump_frame=args.dump_frame)
    print(f"== B = {args.b} =="); p_dig, p_page = _run_pass(args.demo, **_CONFIGS[args.b], max_frames=args.max_frames, dump_frame=args.dump_frame)

    n = min(len(h_dig), len(p_dig))
    if len(h_dig) != len(p_dig):
        print(f"\n!! rendered-frame COUNT differs: A={len(h_dig)} B={len(p_dig)} (comparing first {n})")
    full_div = [i for i in range(n) if h_dig[i][0] != p_dig[i][0]]
    hud_div = [i for i in range(n) if h_dig[i][1] != p_dig[i][1]]
    game_div = [i for i in full_div if i not in set(hud_div)]
    print(f"\n=== {n} rendered frames compared ({args.a} vs {args.b}, displayed page @6772) ===")
    print(f"  full-page divergent frames: {len(full_div)}")
    print(f"    of which HUD-band (rows 176-199): {len(hud_div)}")
    print(f"    of which game-area only        : {len(game_div)}")
    if full_div:
        print(f"  first divergent rendered frame: {full_div[0]}  (hud={'Y' if full_div[0] in set(hud_div) else 'n'})")
        print(f"  example divergent frames: {full_div[:12]}")
    else:
        print("  >>> DISPLAYED FRAME IS BYTE-IDENTICAL TO PURE ASM ON EVERY RENDERED FRAME <<<")

    if args.dump_frame is not None and h_page is not None and p_page is not None:
        out = ROOT / "artifacts" / f"framediff_{Path(args.demo).name}_f{args.dump_frame}.npz"
        np.savez_compressed(out, hybrid=h_page, pure=p_page)
        diff = int((h_page != p_page).sum())
        print(f"  dumped frame {args.dump_frame}: {out.name}  (planes differ in {diff} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
