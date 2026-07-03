"""Regression: the menu-idle ATTRACT title animation [asm 8F2D..9046] — the caveman runs across the
PREHISTORIK-2 jungle (MENU2.SQZ) chased by a dino, then 3 bouncing objects as he runs back.

The driver (638B caveman + 9047 object anim + the two move loops) and the render (plan_frame +
paint_sprite over the decoded planar base) were verified frame-for-frame PIXEL-EXACT against a VM
witness (119 frames, 0 diff; pre2.probes.verify_attract_module). This test pins the full sequence to a
golden hash of every yielded frame's planes, from a pure cold-boot state (no VM, no captured memory)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pre2.bridge.image_scene import title_planar_image
from pre2.native.attract_title import native_attract_title
from pre2.native.cold_boot import native_cold_boot

ROOT = Path(__file__).resolve().parents[1]
_GOLDEN = "7a3a48ce5867b1ae4cd0b028ac475346d53742e8b3fae5becb500a08be2b54f9"


def test_attract_title_base_is_menu2_planar():
    # MENU2.SQZ is a 16-colour PLANAR EGA image (4 x 8000-byte planes + 48-byte palette), NOT a 13h linear image.
    planes, pal6 = title_planar_image("MENU2.SQZ", str(ROOT / "assets"))
    assert len(planes) == 4 and all(len(p) == 8000 for p in planes)
    assert len(pal6) == 0x10 * 3 and all(b < 0x40 for b in pal6)   # 16-entry 6-bit palette


def test_attract_title_sequence_pixel_golden():
    state = native_cold_boot(str(ROOT / "assets"), level=0)
    h = hashlib.sha256()
    frames = 0
    for scene in native_attract_title(state, str(ROOT / "assets")):
        assert scene.mode is not None and len(scene.planes) == 4
        for p in scene.planes:
            h.update(p)
        h.update(scene.page.to_bytes(2, "little"))
        frames += 1
    assert frames == 119                       # phase 1 (69: caveman runs right) + phase 2 (50: runs back)
    assert h.hexdigest() == _GOLDEN


def test_attract_title_phase2_staggers_objects():
    # The phase-2 spawn (8FF2 loop) must STAGGER the 3 bounce objects (X += 0x1E, Y -= 3 each), not stack them:
    # the 8FF2 loop target re-runs the add/sub per object. Check via the final-frame object X spread.
    state = native_cold_boot(str(ROOT / "assets"), level=0)
    d = state.data
    base = 0x1A0F << 4
    list(native_attract_title(state, str(ROOT / "assets")))     # run to completion
    xs = [d[base + o] | (d[base + o + 1] << 8) for o in (0x4F2E, 0x4F40, 0x4F52)]
    assert xs[0] != xs[1] != xs[2] and xs[0] != xs[2]           # distinct positions (staggered)
