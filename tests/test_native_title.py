"""Verification test for the COMPOSED native TITUS title screen (``pre2.native.front_end``).

``_native_title_screen`` composes three already-verified leaves — the 13h image
(``bridge.image_scene.render_image_scene``, Δ=0 vs the framebuffer), the asset palette, and the DAC
fade (``recovered.front_end_fade``, byte-exact vs the VM DAC) — into the title screen's displayed frame
sequence (fade-IN 31 + hold 70 + fade-OUT 16 = 117 frames, the cadence measured off the VM).

The golden below is the SHA-1 of the rendered RGB for all 117 frames. It was proven **byte-for-byte
equal to the VM's actual mode-13h framebuffer (A000) × live DAC palette** over the title sequence on
the near-coldstart demo — i.e. the native title screen displays exactly what the original shows, pixel
for pixel, every frame, with no VM in the loop. This locks the composition with no VM needed.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

from pre2.native.front_end import (
    MODE_LINEAR,
    MODE_PLANAR,
    FrontEndScene,
    _native_present_screen,
    _native_title_screen,
)


def _front_end_scene_to_rgb(scene):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from sdl_view import front_end_scene_to_rgb
    return front_end_scene_to_rgb(scene)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ACE7_FIXTURE = ROOT / "tests" / "fixtures" / "present_morph_ace7.bin"

# SHA-1 of the concatenated 200x320x3 uint8 RGB of every frame — captured from the runs that matched the VM's
# A000 framebuffer × palette byte-for-byte (TITUS 117/117, PRESENT 490/490).
GOLD_TITUS_RGB = "a7c900c8ed75b16e1a82063c76a7e5e2fdd9720e"
GOLD_PRESENT_RGB = "f958c5775934b1fab3dadb3e9a15ec3886a38308"

pytestmark = pytest.mark.skipif(
    not (ASSETS / "TITUS.SQZ").exists(), reason="original PRE2 title asset not present",
)


def _scene_rgb(scene) -> np.ndarray:
    pal = np.array(scene.palette, np.uint8)                       # (256, 3) 8-bit DAC
    img = np.frombuffer(scene.linear, np.uint8).reshape(200, 320)
    return pal[img]


def test_native_titus_screen_byte_exact_vs_vm():
    frames = list(_native_title_screen(str(ASSETS), "TITUS.SQZ", n_entries=0x10, hold=70))
    assert len(frames) == 31 + 70 + 16                            # fade-IN + hold + fade-OUT
    assert all(s.mode == MODE_LINEAR and s.linear is not None for s in frames)
    # the image is fixed across the screen; only the palette animates
    assert len({s.linear for s in frames}) == 1
    rgb = [_scene_rgb(s) for s in frames]
    assert rgb[0].max() == 0                                      # fade-in opens on a black screen
    assert rgb[31 + 35].max() > 0                                 # mid-hold the image is fully lit
    assert rgb[-1].max() == 0                                     # fade-out ends black
    digest = hashlib.sha1(b"".join(r.tobytes() for r in rgb)).hexdigest()
    assert digest == GOLD_TITUS_RGB


@pytest.mark.skipif(not ACE7_FIXTURE.exists() or not (ASSETS / "PRESENT.SQZ").exists(),
                    reason="ACE7 morph-target fixture or PRESENT.SQZ not present")
def test_native_present_screen_byte_exact_vs_vm():
    morph_target = ACE7_FIXTURE.read_bytes()                     # DGROUP 0xACE7 (read from state in the game)
    frames = list(_native_present_screen(str(ASSETS), morph_target))
    assert len(frames) == 256 + 234                              # fade-IN (background) + morph (background+logo)
    assert all(s.mode == MODE_LINEAR for s in frames)
    # the fade-in uses the background-only image, the morph the background+logo image -> two distinct images
    assert len({s.linear for s in frames}) == 2
    rgb = [_scene_rgb(s) for s in frames]
    assert rgb[0].max() == 0                                     # fade-in opens black
    assert rgb[255].max() > 0                                    # background fully lit by the end of the fade-in
    digest = hashlib.sha1(b"".join(r.tobytes() for r in rgb)).hexdigest()
    assert digest == GOLD_PRESENT_RGB


def test_front_end_scene_to_rgb_reproduces_title_golden():
    # the unified present path (sdl_view.front_end_scene_to_rgb) renders a MODE_LINEAR FrontEndScene to the
    # same bytes as the verified title golden.
    frames = list(_native_title_screen(str(ASSETS), "TITUS.SQZ", n_entries=0x10, hold=70))
    rgb = [_front_end_scene_to_rgb(s) for s in frames]
    assert hashlib.sha1(b"".join(r.tobytes() for r in rgb)).hexdigest() == GOLD_TITUS_RGB


def test_front_end_scene_to_rgb_planar_pan():
    # a MODE_PLANAR scene with the 0Dh CRTC pan (pel + 0x1FFF wrap) renders to a full 320x200 RGB frame
    planes = tuple(bytes(0x10000) for _ in range(4))
    palette = tuple((i, i, i) for i in range(256))
    scene = FrontEndScene(MODE_PLANAR, palette=palette, planes=planes, page=0x100, pel=3, wrap=0x1FFF)
    rgb = _front_end_scene_to_rgb(scene)
    assert rgb.shape == (200, 320, 3)
