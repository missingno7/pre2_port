"""Verify pre2.native.attract_title.native_attract_title reproduces the 119-frame VM witness,
seeding from frame0_mem.bin (which carries the loaded sprite bank + attrs + anim scripts)."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from pre2.native.attract_title import native_attract_title    # noqa: E402
from pre2.native.state import NativeGameState                 # noqa: E402
from sdl_view import render_planar_rgb_from_planes            # noqa: E402

OUT = ROOT / "artifacts" / "attract_pixels"


def main():
    st = NativeGameState(bytearray((OUT / "frame0_mem.bin").read_bytes()))
    diffs = []
    for f, scene in enumerate(native_attract_title(st, str(ROOT / "assets"))):
        rgb = np.asarray(render_planar_rgb_from_planes(list(scene.planes), scene.page, scene.palette), np.uint8)
        vm_path = OUT / f"anim_{f:03d}.png"
        if not vm_path.exists():
            print(f"frame {f}: no witness (module produced more frames)"); break
        vm = np.asarray(Image.open(vm_path).convert("RGB"), np.uint8)
        nz = int(np.any(rgb.astype(int) - vm.astype(int) != 0, axis=2).sum())
        diffs.append(nz)
    print(f"frames: {len(diffs)}  matching: {sum(1 for x in diffs if x == 0)}")
    bad = [i for i, x in enumerate(diffs) if x]
    print("diverging frames:", bad[:10], "..." if len(bad) > 10 else "")


if __name__ == "__main__":
    main()
