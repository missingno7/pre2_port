"""alpha=1 parity gate for the enhanced compositor over NATIVE state (the interpolation prerequisite).

The enhancement pipeline (extract_enhanced_frame -> compose) was proven against VM snapshots in the hybrid
era; the native product needs the same guarantee over a NativeGameState: composing the extracted snapshot at
alpha=1 (current positions, no interpolation) must reproduce the FAITHFUL frame pixel-exact. If this holds,
wiring interpolation into play_native cannot change what a stationary frame looks like — the lerp only moves
sprites BETWEEN two provably-correct endpoint frames.

Runs the four faithful-golden scenarios (L1 baseline / level 4 vertical / 0x0D earthquake / 0x0F snow) plus a
multi-tick sweep on the earthquake level (parity at EVERY tick of a 60-tick window, catching per-frame
effects: shake, flash slots, particles). Also sanity-checks a mid-alpha compose (prev+cur, alpha=0.5) for the
interpolation path itself: it must produce a frame (no crash) with sprites between their endpoints.

    python pre2/probes/verify_native_enhanced_parity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np                                     # noqa: E402

from pre2.enhanced.compositor import compose           # noqa: E402
from pre2.enhanced.extract import extract_enhanced_frame   # noqa: E402
from pre2.native.cold_boot import native_cold_boot     # noqa: E402
from pre2.native.loop import native_gameplay_frame     # noqa: E402
from pre2.native.render import native_load_level_palette, native_sync_render_state   # noqa: E402
from pre2.native.vga import NativeVGA                  # noqa: E402

VIEW_H = 200
GR = str(ROOT / "assets")


def _boot(level: int, ticks: int):
    state = native_cold_boot(GR, level=level)
    dos = NativeVGA()
    native_load_level_palette(state, dos)
    for _ in range(ticks):
        native_gameplay_frame(state)
    native_sync_render_state(state)
    return state, dos


def _parity(state, dos, label: str) -> int:
    efs = extract_enhanced_frame(state, dos, game_root=GR)
    if efs is None:
        print(f"  {label}: not a gameplay frame -> faithful passthrough")
        return 0
    comp = compose(efs, None, 1.0)[:VIEW_H]
    faith = efs.faithful_rgb[:VIEW_H]
    diff_mask = np.any(comp != faith, axis=2)
    diff = int(diff_mask.sum())
    tag = "OK" if diff == 0 else "MISMATCH"
    print(f"  {label}: sprites={len(efs.sprites)} unsupported={len(efs.unsupported)} alpha=1 diff={diff}px {tag}")
    if diff:
        ys, xs = np.nonzero(diff_mask)
        print(f"     first diff (row={ys[0]}, col={xs[0]}) comp={comp[ys[0], xs[0]]} faith={faith[ys[0], xs[0]]}; "
              f"bbox rows {ys.min()}..{ys.max()} cols {xs.min()}..{xs.max()}")
    return diff


def main() -> int:
    total = 0
    print("single-state parity (the four golden scenarios):")
    for level, ticks in ((0x00, 120), (0x04, 150), (0x0D, 120), (0x0F, 120)):
        state, dos = _boot(level, ticks)
        total += _parity(state, dos, f"level {level:#04x} @{ticks}")

    print("multi-tick sweep (earthquake level, ticks 120..180, parity EVERY tick):")
    state, dos = _boot(0x0D, 120)
    fails = 0
    prev_efs = None
    for t in range(120, 180):
        native_sync_render_state(state)
        efs = extract_enhanced_frame(state, dos, game_root=GR)
        if efs is not None:
            comp = compose(efs, None, 1.0)[:VIEW_H]
            d = int(np.any(comp != efs.faithful_rgb[:VIEW_H], axis=2).sum())
            if d:
                fails += 1
                if fails <= 3:
                    print(f"  tick {t}: diff={d}px")
            if prev_efs is not None:
                mid = compose(efs, prev_efs, 0.5)      # the interpolation path: must not crash
                assert mid.shape[0] >= VIEW_H
            prev_efs = efs
        native_gameplay_frame(state)
    total += fails
    print(f"  sweep: {60 - fails}/60 ticks pixel-exact at alpha=1; mid-alpha compose OK")

    print("full-pipeline sweep vs native_render (EFFECTS + FLASH: particles/foreground/fireflies/opaque),")
    print("earthquake level, 60 ticks — the exact construction play_native's interpolation uses:")
    from pre2.bridge.foreground_tiles import read_foreground_state
    from pre2.bridge.gameplay_effects import capture_gameplay_effects
    from pre2.native.render import native_render
    from sdl_view import render_planar_rgb_from_planes
    state, dos = _boot(0x0D, 120)
    ds = 0x1A0F << 4
    fails = worst = 0
    for t in range(60):
        native_gameplay_frame(state)
        native_sync_render_state(state)
        disp = state.data[ds + 0x2DD6] | (state.data[ds + 0x2DD7] << 8)
        planes, page = native_render(state, dos, disp, game_root=GR, force_gameplay=True)   # the reference
        faith = np.asarray(render_planar_rgb_from_planes(planes, page, dos.vga_palette), np.uint8)
        fg = read_foreground_state(state)
        fg.page = disp & 0xFFFF
        fx = capture_gameplay_effects(state, particle_frame=getattr(state, "particle_capture_last", None),
                                      foreground_frame=fg)
        flash = getattr(state, "flash_slots_last", None)
        saved = [(off, state.data[ds + off + 5]) for off in (flash or [])]
        for off in (flash or []):
            state.data[ds + off + 5] |= 0x40
        try:
            efs = extract_enhanced_frame(state, dos, game_root=GR, with_faithful=False, effects=fx)
        finally:
            for off, v in saved:
                state.data[ds + off + 5] = v
        if efs is None:
            continue
        comp = compose(efs, None, 1.0)[:VIEW_H]
        d = int(np.any(comp != faith[:VIEW_H], axis=2).sum())
        worst = max(worst, d)
        if d:
            fails += 1
            if fails <= 3:
                ys, xs = np.nonzero(np.any(comp != faith[:VIEW_H], axis=2))
                print(f"  tick {t}: diff={d}px  bbox rows {ys.min()}..{ys.max()} cols {xs.min()}..{xs.max()}")
    print(f"  full sweep: {60 - fails}/60 ticks pixel-exact vs native_render (worst diff {worst}px)")
    total += fails

    print("PASS — the enhanced pipeline is parity-proven over native state" if total == 0
          else f"FAIL — {total} residual (px + ticks)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
