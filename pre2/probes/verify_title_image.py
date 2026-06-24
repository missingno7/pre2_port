"""Verify the recovered title 13h image (1030:91A4 + 9090) Δ=0 vs the ASM framebuffer.

The ASM composites the title from PRESENT.SQZ (background copy 91A4 + logo-top overlay 9090) into the
mode-13h A000 linear framebuffer. This probe decodes PRESENT.SQZ with the recovered ``unpack_sqz``, runs
the recovered ``render_title_image``, and asserts it matches the displayed title framebuffer (the ASM
oracle, snapshot intro_image_163804) byte-exact.
"""
import glob
import sys

sys.path.insert(0, ".")

from pre2.codecs.sqz import unpack_sqz
from pre2.recovered.title_image import render_title_image, title_background, title_logo_top
from pre2.runtime import load_pre2_snapshot


def main(snap=None, asset="assets/PRESENT.SQZ"):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*intro_image_20260622_163804")[0]
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    a000 = bytes(rt.program.memory.data[0xA0000:0xA0000 + 64000])     # the ASM-composited title

    with open(asset, "rb") as f:
        decoded = unpack_sqz(f.read())
    img = render_title_image(decoded)

    diff = sum(1 for i in range(64000) if img[i] != a000[i])
    # also report the two layers separately (base only vs base+logo)
    base = bytearray(title_background(decoded))
    base_diff = sum(1 for i in range(64000) if base[i] != a000[i])
    logo_px = sum(1 for b in title_logo_top(decoded))
    print(f"title asset decoded {len(decoded)} bytes; logo-top {logo_px} bytes")
    print(f"  background only vs displayed: Δ={base_diff} (the logo region)")
    print(f"  background + logo-top vs displayed: Δ={diff}")
    ok = diff == 0
    print("TITLE_IMAGE: PASS" if ok else "TITLE_IMAGE: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
