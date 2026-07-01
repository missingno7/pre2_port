"""The VM-less cold boot (``pre2.native.cold_boot``) — assembling a level from the game files with no emulator.

Proves the endgame runtime: from the static boot image (extracted once by the VM, then bundled as data) +
the GOG asset files, the recovered Python loaders build Level 1 and it renders byte-identical to the VM path
(``artifacts/coldboot_L1.png``) — no x86 interpreted, no ``PRE2.EXE`` executed at runtime.

The boot image is generated from ``PRE2.EXE`` on first run and cached under ``artifacts/`` (it is the game's
own static data, not committed). The render golden below was captured from the VM-less path.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

from pre2.native.cold_boot import build_boot_image, native_cold_boot

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
EXE = ASSETS / "pre2.exe"
BOOT_IMAGE = ROOT / "artifacts" / "pre2_boot_image.zz"
_DS = 0x1A0F << 4

GOLD_L1_RGB = "e21ca0fa63cb380e649f2cff22a3182768a94ef0"   # the VM-less L1 frame (== the VM-image path, 0px diff)

pytestmark = pytest.mark.skipif(
    not EXE.exists() or not (ASSETS / "SPRITES.SQZ").exists(),
    reason="PRE2.EXE / assets not present",
)


def _ensure_boot_image() -> str:
    if not BOOT_IMAGE.exists():
        BOOT_IMAGE.parent.mkdir(parents=True, exist_ok=True)
        build_boot_image(str(EXE), str(BOOT_IMAGE), game_root=str(ASSETS))   # the VM's one build-time use
    return str(BOOT_IMAGE)


def _render_l1(state) -> np.ndarray:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from sdl_view import render_planar_rgb_from_planes
    from dos_re.dos import DOSMachine
    from pre2.native.render import native_load_level_palette, native_render
    dos = DOSMachine(str(ASSETS))
    native_load_level_palette(state, dos)
    disp = state.data[_DS + 0x2DD6] | (state.data[_DS + 0x2DD7] << 8)
    planes, page = native_render(state, dos, disp, game_root=str(ASSETS))
    return np.asarray(render_planar_rgb_from_planes(planes, page, dos.vga_palette), np.uint8)


def test_native_cold_boot_renders_l1_with_no_vm():
    # RUNTIME path: no create_pre2_runtime / no cpu — just the bundle + the native loaders.
    state = native_cold_boot(str(ASSETS), _ensure_boot_image(), level=0)
    rgb = _render_l1(state)
    assert rgb.shape == (200, 320, 3)
    assert int((rgb.sum(2) > 0).sum()) == 54863          # the level is actually drawn (~86% non-black)
    assert len(np.unique(rgb.reshape(-1, 3), axis=0)) == 15
    assert hashlib.sha1(rgb.tobytes()).hexdigest() == GOLD_L1_RGB


def test_boot_image_round_trips():
    from pre2.native.cold_boot import load_boot_image
    image = load_boot_image(_ensure_boot_image())
    assert len(image) >= 0x100000                        # the full real-mode address space
    assert image[(0x1A0F << 4) + 0x7190]                 # the static [0x7190] sprite-descriptor table is present
    # captured at the OLDIES entry (240A), AFTER main's boot init: the font seg [0x3D] is loaded (nonzero) so the
    # front-end OLDIES/menu text renders. A capture at main entry (00A0) would leave it 0 -> garbage glyphs.
    assert image[(0x1A0F << 4) + 0x3D] or image[(0x1A0F << 4) + 0x3E]


def _sx(state, off):                                     # signed 16-bit DGROUP word
    v = state.data[_DS + off] | (state.data[_DS + off + 1] << 8)
    return v - 0x10000 if v >= 0x8000 else v


def test_native_cold_boot_is_playable_with_no_vm():
    # Beyond rendering: the cold-boot state must actually PLAY — host input drives the recovered gameplay with no
    # emulator. This guards the boot-time keyboard-input config (init_keyboard_input: the joystick-detect outcome
    # that lets DC1 reach the keyboard); without it DC1 fails loud on the port-0x201 read and the player is frozen.
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.input import apply_input
    from pre2.native.loop import native_gameplay_frame

    state = native_cold_boot(str(ASSETS), _ensure_boot_image(), level=0)
    assert state.data[_DS + 0x27E4] == 0xFF              # joystick marked absent -> DC1 reads the keyboard
    x0 = _sx(state, 0x4F1C)
    for _ in range(40):                                 # hold RIGHT; the VM-less gameplay step must complete each
        apply_input(state, right=True)                  #   frame (no Pre2HybridGap) and walk the player right
        native_gameplay_frame(state)
    assert _sx(state, 0x4F1C) - x0 > 100                # the player advanced (input -> FSM -> X-integrate, VM-less)
    assert state.data[_DS + 0x27EC] == 0xFF             # the decoded "right" FSM flag is live


def test_native_front_end_oldies_palette():
    # The OLDIES credits screen loads its OWN 16-colour palette (the green/yellow look) from the DGROUP table at
    # 0x287e (0b92 -> int10 AX=1012), NOT the default EGA palette. This guards native_load_dac_palette being wired
    # into the front-end; the whole OLDIES frame is pixel-exact vs the VM (verified separately with the emulator).
    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import load_boot_image
    from pre2.native.front_end import native_front_end
    from pre2.native.input import init_keyboard_input
    from pre2.native.state import NativeGameState

    state = NativeGameState(load_boot_image(_ensure_boot_image()))
    init_keyboard_input(state)
    scene = next(native_front_end(state, DOSMachine(str(ASSETS)), 0, game_root=str(ASSETS)))
    assert scene.palette[2] == (0, 243, 0)              # green (0x287e entry 2), not the default EGA (0, 170, 0)
    assert scene.palette[5] == (243, 243, 113)          # yellow (0x287e entry 5)


def test_menu_press12_screen_render():
    # The "press 1/2" difficulty screen (8e45: MENU.SQZ = resource 8) is a 13h image faded in over 0xA0 DAC
    # entries, the same leaf as the titles. Golden-locks the fully-faded frame (verified pixel-exact vs the VM's
    # A000xDAC separately). The '1'/'2' dispatch itself is covered by tests/test_world_map.
    import hashlib

    from pre2.bridge.image_scene import image_palette, render_image_scene
    from pre2.native.front_end import _expand_palette6
    from pre2.recovered.front_end_fade import fade_in_frames

    img = np.frombuffer(render_image_scene("MENU.SQZ", str(ASSETS)), np.uint8).reshape(200, 320)
    pal = np.array(_expand_palette6(fade_in_frames(image_palette("MENU.SQZ", str(ASSETS)), 0xA0)[-1]), np.uint8)
    assert hashlib.sha1(pal[img].tobytes()).hexdigest() == "6dd4eae081aa675ca3753ab39913872548edf96f"


def test_native_menu_map_scroll_pixel_exact():
    # The 96d5 scrolling world-map (the '2'/password screen): MOTIF.SQZ caveman tiles scrolling left (the stateful
    # self-copy) + "ENTER CODE" / code text stamped each frame via the bit-rotated font (seed part B). Verified
    # 0-diff vs the VM's A000 planes (all 4 planes, every captured frame); this golden-locks the composed frames.
    import hashlib
    import itertools

    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import load_boot_image
    from pre2.native.front_end import _native_menu_map
    from pre2.native.input import init_keyboard_input
    from pre2.native.state import NativeGameState

    state = NativeGameState(load_boot_image(_ensure_boot_image()))
    init_keyboard_input(state)
    h = hashlib.sha1()
    for scene in itertools.islice(_native_menu_map(state, DOSMachine(str(ASSETS)), str(ASSETS), "password"), 30):
        for plane in scene.planes:
            h.update(bytes(plane)[:0x2000])
    assert h.hexdigest() == "091655f679b9a30a2d53fdf5426d48b12265f132"
