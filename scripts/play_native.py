"""Play Prehistorik 2 with the VM-LESS native core — COLD BOOT from the OLDIES screen through the whole flow.

The standalone runner, with NO emulator anywhere: from a pre-extracted static boot image (the EXE's init memory,
the VM's only build-time use) + the GOG ``*.SQZ`` assets, it drives the recovered FRONT-END flow (OLDIES credits ->
TITUS title -> PREHISTORIK-2 title -> menu -> world map -> level) and then the recovered GAMEPLAY — no x86 is
interpreted and ``PRE2.EXE`` is never executed at runtime. This is the VM-less counterpart of ``play.py --view``:
it starts at the very first screen, exactly like the real game, and runs forward until it hits a not-yet-recovered
gap (where it stops and reports, rather than silently faking anything).

    python scripts/play_native.py                  # full cold start: OLDIES -> titles -> ... (the real boot)
    python scripts/play_native.py --from-level 0    # DEBUG: skip the front-end, drop straight into LEVEL1 gameplay
    python scripts/play_native.py --fps 30          # gameplay tick rate (front-end runs at its native 70Hz)

Controls: SPACE = advance the OLDIES screen / fire+jump in game; arrow keys / numpad = move; ESC = quit.

THE BOOT IMAGE is the EXE's initialized memory at the ``main`` entry, extracted ONCE by the VM (its only role, a
build tool) and cached under ``artifacts/``; it is built automatically on first run if absent (the one build-time
use of ``PRE2.EXE``). Copy the package + the boot image + ``assets/`` anywhere and run.

STATUS: the front-end drives OLDIES + the two title screens byte-exact VM-less, then stops at the menu/world-map
gap (still being recovered — #14). ``--from-level`` boots a level directly for gameplay testing (the front-end
sets up per-level state the direct path approximates, so a directly-booted level is functional but not a
substitute for the real flow). When the runner reaches an unrecovered gap it prints it and holds the last frame.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

DS = 0x1A0F << 4
_BOOT_IMAGE = ROOT / "artifacts" / "pre2_boot_image.zz"
_FRONT_END_FPS = 70           # the front-end runs at the VGA retrace rate (its FrontEndScene frames are per-retrace)


def _ensure_boot_image(boot_image: Path) -> str:
    """Return the boot-image path, building it from PRE2.EXE on first run (the VM's only, build-time use)."""
    if not boot_image.exists():
        from pre2.native.cold_boot import build_boot_image
        exe = ROOT / "assets" / "pre2.exe"
        if not exe.exists():
            raise SystemExit(f"no boot image at {boot_image} and no PRE2.EXE at {exe} to build it from")
        boot_image.parent.mkdir(parents=True, exist_ok=True)
        print(f"building the boot image from {exe.name} (one-time; the runtime stays VM-less)...")
        size = build_boot_image(str(exe), str(boot_image), game_root=str(ROOT / "assets"))
        print(f"  wrote {boot_image} ({size} bytes)")
    return str(boot_image)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Play PRE2 with the VM-less native core (cold boot from OLDIES)")
    ap.add_argument("--from-level", type=int, default=None,
                    help="DEBUG: skip the front-end and boot this 0-based level directly (0 -> LEVEL1)")
    ap.add_argument("--boot-image", default=str(_BOOT_IMAGE),
                    help="the static boot image (the EXE's init memory); built from PRE2.EXE if absent")
    ap.add_argument("--fps", type=int, default=24,
                    help="gameplay tick-rate cap; default ~24Hz (the main loop waits 3 VGA retraces)")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args(argv)

    import numpy as np
    import pygame
    from dos_re.dos import DOSMachine
    from pre2.checkpoints.common import Pre2HybridGap
    from pre2.native.cold_boot import load_boot_image, native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.input import apply_input, init_keyboard_input
    from pre2.native.render import native_load_level_palette
    from pre2.native.runtime import native_frame_step
    from pre2.native.state import NativeGameState
    from sdl_view import front_end_scene_to_rgb, render_planar_rgb_from_planes

    gr = str(ROOT / "assets")
    boot_image = _ensure_boot_image(Path(args.boot_image))

    pygame.init()
    screen = pygame.display.set_mode((320 * args.scale, 200 * args.scale))
    clock = pygame.time.Clock()
    ref = {"running": True, "last": None}

    def pump():
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                ref["running"] = False

    def present(rgb, fps, caption=None):
        surf = pygame.surfarray.make_surface(np.asarray(rgb, np.uint8).swapaxes(0, 1))
        screen.blit(pygame.transform.scale(surf, screen.get_size()), (0, 0))
        pygame.display.flip()
        clock.tick(fps)
        if caption:
            pygame.display.set_caption(caption)
        ref["last"] = rgb

    def hold_last(msg):
        """An unrecovered gap (or a finished run): print once, hold the last frame until the user quits."""
        print(f"  {msg}")
        pygame.display.set_caption(f"PRE2 VM-less — {msg[:80]}")
        while ref["running"]:
            pump()
            if ref["last"] is not None:
                present(ref["last"], 30)
            else:
                clock.tick(30)

    # ---- audio: the recovered ENHANCED player (VM-free), driven by the native frame's audio commands ----
    native_audio = None
    try:
        from sdl_view import SdlEnhancedAudio
        from pre2.native.audio import NativeAudio
        native_audio = NativeAudio(SdlEnhancedAudio(pygame, gr, {}).post, gr)
    except Exception as e:                                          # noqa: BLE001 — no audio device -> run silent
        print(f"  (audio disabled: {type(e).__name__}: {str(e)[:60]})")

    def gameplay_loop(state, dos):
        """Run the recovered gameplay VM-less: host input -> native_frame_step -> present, until a gap."""
        print("Gameplay — SPACE = fire/jump, arrows/numpad = move, ESC = quit. (VM-less native gameplay)")
        n = 0
        while ref["running"]:
            pump()
            k = pygame.key.get_pressed()
            apply_input(state, left=k[pygame.K_LEFT] or k[pygame.K_KP4],
                        right=k[pygame.K_RIGHT] or k[pygame.K_KP6],
                        up=k[pygame.K_UP] or k[pygame.K_KP8],
                        down=k[pygame.K_DOWN] or k[pygame.K_KP2], fire=k[pygame.K_SPACE])
            disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
            try:
                for planes, page in native_frame_step(state, dos, disp, game_root=gr):
                    rgb = render_planar_rgb_from_planes(planes, page, dos.vga_palette)
                    n += 1
                    present(rgb, args.fps, None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")
                    pump()
                    if not ref["running"]:
                        break
            except Exception as e:                                  # noqa: BLE001 — hold on an unrecovered gap
                hold_last(f"gameplay gap: {type(e).__name__}: {str(e)[:80]}")
                return
            if native_audio is not None:
                native_audio.poll(state)

    if args.from_level is not None:
        # ---- DEBUG path: jump straight into a level for gameplay testing (no front-end) ----
        print(f"--from-level {args.from_level}: booting LEVEL{args.from_level + 1} directly (VM-less, no front-end)...")
        state = native_cold_boot(gr, boot_image, level=args.from_level)
        dos = DOSMachine(gr)
        native_load_level_palette(state, dos)
        gameplay_loop(state, dos)
        pygame.quit()
        return 0

    # ---- the real cold start: OLDIES -> titles -> menu -> map -> level, all VM-less ----
    print("Cold boot from the OLDIES screen (VM-less). SPACE to advance, ESC to quit...")
    state = NativeGameState(load_boot_image(boot_image))
    init_keyboard_input(state)                                     # the boot joystick-detect outcome (DC1 input)
    dos = DOSMachine(gr)
    reached_gameplay = False
    try:
        for scene in native_front_end(state, dos, 0, game_root=gr):
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — cold boot (front-end)")
            pump()
            k = pygame.key.get_pressed()
            apply_input(state, fire=k[pygame.K_SPACE])             # OLDIES scene-wait (0bbe) reads the fire key
            if not ref["running"]:
                break
        reached_gameplay = ref["running"]                          # the generator finished -> a level started
    except Pre2HybridGap as e:
        hold_last(f"front-end reached a not-yet-recovered gap: {str(e)[:110]}")
    except Exception as e:                                         # noqa: BLE001
        hold_last(f"front-end error: {type(e).__name__}: {str(e)[:90]}")

    if reached_gameplay and ref["running"]:
        native_load_level_palette(state, dos)
        gameplay_loop(state, dos)

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
