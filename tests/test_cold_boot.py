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

# the VM-less cold-boot L1 frame. Includes the level-start block (lives=2), FRONT.SQZ (level at seg 0x5cc1), the
# 3ead secret-tile hide, and the PARALLAX backdrop: BACK0.SQZ decoded into the 0x7E80 base the renderer composites
# behind transparent tiles (100% == the VM's 0x7E80; the sky is now blue instead of black).
GOLD_L1_RGB = "377100ae7085a81133d9181c7232c0148595d8bf"

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
    assert int((rgb.sum(2) > 0).sum()) == 63680          # the level is drawn incl. the blue parallax sky (~99%)
    assert len(np.unique(rgb.reshape(-1, 3), axis=0)) == 16
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


def test_native_cold_boot_computes_bios_password_seed():
    # The BIOS-ROM password seed [0xA333] (932F's one-time init, guarded by the [0xA335] "computed" flag) must be
    # computed VM-less during the level load -- the 40bd decor assignment is the first 932F call, exactly as in the
    # VM. On the zeroed-BIOS GOG build it is the 0x20 fallback, matching the VM's captured gameplay-entry state
    # ([0xA333]=0x20). Without it the random decor sprites ([0x8f1d]+4) diverge from the VM. Proven by diffing the
    # native level-load against the pure-ASM oracle's 0214 gameplay-entry seed (all core gameplay tables byte-exact).
    state = native_cold_boot(str(ASSETS), _ensure_boot_image(), level=0)
    assert state.data[_DS + 0xA333] == 0x20 and state.data[_DS + 0xA334] == 0
    assert state.data[_DS + 0xA335] == 1               # the one-time "seed computed" flag is set


GOLD_YOFF_TABLE = "dcf7223ce05222c1a8439be54f7d91c50da60445"   # [0x752A..0x772A] after the 2E15 bottom-anchor fixup


def test_native_cold_boot_bottom_anchors_sprite_y_offsets():
    # The sprite-bank head (2E15-2E29) rewrites each per-sprite Y draw-offset [0x752B] = height - y_off (a ONE-TIME
    # cold-boot fixup). The renderer's baseline placement subtracts height again, so WITHOUT this every body sprite
    # (player + enemies) draws exactly one sprite-height too LOW (sunk into the ground); the club, a zero-height
    # attachment, is unaffected. The transformed table is byte-exact vs the VM's captured L1 gameplay-entry state.
    from pre2.recovered.sprite_bank import bottom_anchor_y_offsets

    state = native_cold_boot(str(ASSETS), _ensure_boot_image(), level=0)
    d = state.data
    table = bytes(d[_DS + 0x752A:_DS + 0x772A])
    assert hashlib.sha1(table).hexdigest() == GOLD_YOFF_TABLE
    # the ground-anchoring is real: the first sprites' Y offsets are ~0 (baseline == feet), not ~height
    yoffs = [d[_DS + 0x752B + i * 2] for i in range(3)]
    assert yoffs == [0, 0, 0], f"expected bottom-anchored y_offs ~0, got {yoffs}"

    # the pure transform: y_off = height - y_off per id, x_off untouched, stops at the zero-height terminator
    desc = bytes([0, 36, 0, 35, 0, 0])            # id0 height 36, id1 height 35, id2 height 0 = terminator
    draw = bytes([20, 36, 16, 35, 12, 99])
    assert bottom_anchor_y_offsets(desc, draw) == bytes([20, 0, 16, 0, 12, 99])


def test_native_front_end_cold_start_reaches_gameplay():
    # THE cold-start proof: drive the WHOLE VM-less front-end (OLDIES -> TITUS -> PRESENT -> "press 1/2" ->
    # mode-select) with scripted host input; it must reach the level-init handoff -- the generator RETURNS, so the
    # runner switches to native_frame_step -- with the player + object pool BYTE-IDENTICAL to the verified
    # native_cold_boot(level=0). That equality proves the front-end's residual state does NOT perturb the level load
    # (the load is segment-relative + re-inits the gameplay tables), so gameplay starts with no divergence. The load
    # itself is verified byte-exact vs the pure-ASM gameplay-entry oracle (see test_native_cold_boot_* / the island).
    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import load_boot_image, native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.input import apply_input, init_keyboard_input, set_key
    from pre2.native.state import NativeGameState

    state = NativeGameState(load_boot_image(_ensure_boot_image()))
    init_keyboard_input(state)
    gen = native_front_end(state, DOSMachine(str(ASSETS)), 0, game_root=str(ASSETS))
    reached = False
    for i, _scene in enumerate(gen):
        set_key(state, 0x02, True)                     # hold '1' -> the menu picks the mode-select map
        apply_input(state, fire=(i % 2 == 0))          # pulse fire: advances OLDIES (press->release) + confirms
        if i > 4000:
            break                                      # safety cap (a real run reaches the handoff in ~900 frames)
    else:
        reached = True                                 # the for-loop exhausted == the generator returned == handoff
    assert reached, "the cold-start front-end never reached the level-init handoff"
    assert state.data[_DS + 0xA333] == 0x20            # the BIOS password seed was computed during the level load
    ref = native_cold_boot(str(ASSETS), _ensure_boot_image(), level=0)
    assert state.data[_DS + 0x4F0A:_DS + 0x5732] == ref.data[_DS + 0x4F0A:_DS + 0x5732]   # player + object pool
    assert (state.data[_DS + 0x4F1C] | (state.data[_DS + 0x4F1D] << 8)) == 0x2A            # player X = level start


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


def test_native_menu_map_renders():
    # The 96d5 scrolling world-map (MOTIF.SQZ caveman tiles + the sine-bounce + the screen text). The BACKGROUND
    # planes 0,1 + the vertical bounce are byte-exact vs the pure-ASM oracle; the TEXT layer (planes 2,3) is a
    # VISUAL APPROXIMATION for now (the VM's double-buffer text handling during the bounce isn't reproduced exactly,
    # so we clear + re-stamp the text each frame to avoid ghosting). This golden just locks the composed output.
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
    assert h.hexdigest() == "a1de3afa1e68e0c630644022b2660d8632e75892"


def test_native_menu_map_mode_select_toggle():
    # The mode-select map: an arrow toggles BEGINNER<->EXPERT ([0xB197], edge-detected — no re-toggle while held);
    # fire commits the difficulty ([0xB198]/[0x83D]) and starts level 1 ([0x2D8A]=0), returning from the generator.
    from dos_re.dos import DOSMachine
    from pre2.native.cold_boot import load_boot_image
    from pre2.native.front_end import _native_menu_map
    from pre2.native.input import init_keyboard_input, set_key
    from pre2.native.state import NativeGameState

    state = NativeGameState(load_boot_image(_ensure_boot_image()))
    init_keyboard_input(state)
    gen = _native_menu_map(state, DOSMachine(str(ASSETS)), str(ASSETS), "mode_select")
    next(gen); next(gen)                                # seed + first frame
    assert state.data[_DS + 0xB197] == 0               # BEGINNER
    set_key(state, 0x48, True); next(gen)              # up-arrow -> EXPERT
    assert state.data[_DS + 0xB197] == 1
    next(gen); next(gen)                                # arrow still held -> NO re-toggle (edge)
    assert state.data[_DS + 0xB197] == 1
    set_key(state, 0x48, False); set_key(state, 0x39, True)   # release, press fire
    try:
        next(gen)                                       # confirm -> the generator returns
        raise AssertionError("expected the generator to return on confirm")
    except StopIteration:
        pass
    assert state.data[_DS + 0x2D8A] == 0               # level 1
    assert state.data[_DS + 0xB198] == 1               # EXPERT difficulty committed


# the carte 'you are here' marker (9543-95CD): the map master with the player-sprite marker stamped on. The golds are
# the byte-exact VM values -- STAMPED matches the VM's captured carte master (the 414-byte 0.6% overlay over MAP.SQZ);
# the marker source is the PLAYER sprite (caveman) itself, at [0x667a]:[0x62da], set up by the front-end's sprite bank.
GOLD_CARTE_MASTER_STAMPED = "e3c3ee6ee7c506009db5cc527505a7735e6b4701"
GOLD_CARTE_MARKER_SPRITE = "399a87730fd7bff5d038ede8c09c01ef1a520015"


def test_native_carte_marker_stamp_matches_vm():
    # The carte's per-level marker (the player's position on the world map) is de-planarized from [0x667a]:[0x62da]
    # onto the map master at di = ((y-0x20)&0xFF)*0x50 + (x>>3), for the level's (x,y) = [0xB148/B14A + level*4]. The
    # marker source is the PLAYER SPRITE itself (loaded by the front-end sprite bank, NOT present in a bare cold boot),
    # so this drives the WHOLE front-end and captures the stamped master _native_carte actually feeds build_carte_page,
    # proving native's chain (marker provenance + position + stamp) is BYTE-EXACT vs the VM's captured carte master.
    from dos_re.dos import DOSMachine
    import pre2.recovered.carte as carte
    from pre2.native.cold_boot import load_boot_image
    from pre2.native.front_end import native_front_end
    from pre2.native.input import apply_input, init_keyboard_input, set_key
    from pre2.native.state import NativeGameState

    cap = {}
    orig = carte.stamp_carte_marker
    def spy(asset, marker, di, w, h):                       # capture the stamp inputs + output from the real flow
        out = orig(asset, marker, di, w, h)
        cap.setdefault("stamped", out)
        cap.setdefault("marker", (di, w, h, hashlib.sha1(marker).hexdigest()))
        return out
    carte.stamp_carte_marker = spy
    try:
        state = NativeGameState(load_boot_image(_ensure_boot_image()))
        init_keyboard_input(state)
        gen = native_front_end(state, DOSMachine(str(ASSETS)), 0, game_root=str(ASSETS))
        for i, _scene in enumerate(gen):
            set_key(state, 0x02, True)                     # hold '1' -> mode-select map
            apply_input(state, fire=(i % 2 == 0))          # pulse fire: advance OLDIES + confirm the mode-select
            if "stamped" in cap or i > 4000:
                break
    finally:
        carte.stamp_carte_marker = orig

    assert "stamped" in cap, "the front-end never reached the carte marker stamp"
    di, w, h, marker_sha = cap["marker"]
    assert (di, w, h) == (0x1090, 5, 34)                   # level 1 -> the map's far left, a 40x34 sprite
    assert marker_sha == GOLD_CARTE_MARKER_SPRITE          # the marker source IS the player sprite (caveman)
    assert hashlib.sha1(cap["stamped"]).hexdigest() == GOLD_CARTE_MASTER_STAMPED
