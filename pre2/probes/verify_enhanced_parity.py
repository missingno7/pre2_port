"""alpha=1 parity gate for the modern enhanced object compositor (build-order step 5).

For each gameplay witness: extract the modern EnhancedFrameState, compose at alpha=1 (current positions,
no interpolation), and compare the RGB result to the faithful frame. They must match over the gameplay
viewport (the compositor's bg+sprites == the faithful bg+sprites). A nonzero diff = a missing/incorrect
layer -> report it, do NOT paper over it with VM reads or per-subframe planar rasterization.
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np
from pre2.enhanced.compositor import compose
from pre2.enhanced.extract import extract_enhanced_frame
from pre2.runtime import load_pre2_snapshot

VIEW_H = 200   # whole frame (viewport 176 + HUD 24); bg_rgb carries the recovered HUD strip


def check(label, snap):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    efs = extract_enhanced_frame(rt.cpu.mem, rt.dos, game_root="assets")
    if efs is None:
        print(f"  {label}: not a gameplay frame (no object camera) -> faithful passthrough"); return 0
    comp = compose(efs, None, 1.0)[:VIEW_H]
    faith = efs.faithful_rgb[:VIEW_H]
    diff_mask = np.any(comp != faith, axis=2)
    diff = int(diff_mask.sum())
    nsup = len(efs.sprites)
    unsup = efs.unsupported
    print(f"  {label}: sprites={nsup} unsupported(OPAQUE/ERASE)={len(unsup)}{unsup if unsup else ''}  "
          f"alpha=1 diff={diff}px {'OK' if diff == 0 else 'MISMATCH'}")
    if diff:
        ys, xs = np.nonzero(diff_mask)
        print(f"     first diff at (row={ys[0]}, col={xs[0]}) comp={comp[ys[0], xs[0]]} faith={faith[ys[0], xs[0]]}; "
              f"bbox rows {ys.min()}..{ys.max()} cols {xs.min()}..{xs.max()}")
    return diff


def main():
    total = 0
    for label, snap in (
        ("SPIDERS 112313", "artifacts/snapshot_pre2_spiders_20260624_112313"),
        ("PLAYER-DEATH 103048", "artifacts/snapshot_pre2_player_death_20260624_103048"),
        ("GAMEPLAY 185902", "artifacts/snapshot_pre2_gameplay_20260621_185902"),
        ("BOSS 192126", "artifacts/snapshot_pre2_20260623_192126"),
    ):
        total += check(label, snap)
    print("ENHANCED alpha=1 PARITY:", "PASS" if total == 0 else f"FAIL ({total}px total)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
