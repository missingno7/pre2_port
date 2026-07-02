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

STATUS: the front-end drives OLDIES + the two title screens + the "press 1/2" menu + the mode-select world-map +
the CARTE map scroll-in VM-less, then hands off to GAMEPLAY: the level-load is verified byte-exact vs the pure-ASM
oracle's gameplay-entry seed (every core gameplay table identical), the secret/bonus tiles are hidden (3ead),
lives/tally are set, the parallax sky is drawn (BACK0.SQZ -> the 0x7E80 base), and PRESENTA/menu/level music plays,
so selecting a difficulty shows the carte (map scroll-in with the player's 'you are here' marker) and loads Level 1
with the correct backdrop and no state divergence. Remaining gaps: SFX (native skips play_sfx's [0x1004] writes) and
the 88D7 combat pass (can't hurt enemies). ``--from-level`` boots a level directly for testing.
When the runner reaches an unrecovered gap it prints it and holds the last frame.
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


class DemoInput:
    """Replay a recorded input demo's scancodes into the VM-less runtime for HANDS-FREE watching.

    A recorded demo is a list of make/break scancode events keyed to a per-frame ``boundary`` counter (the
    hybrid recorder's present-frame index). Here the boundary is advanced once per NATIVE displayed frame and the
    make/break events are turned back into a held-key set, which the runtime writes into DC1's key table exactly
    as a live keyboard would. This is APPROXIMATE across the front-end (native scene timing differs from the
    recording, so menu/title waits can drift); it is faithful for gameplay, where the frame is the game tick.
    Live keys are merged on top, so you can always nudge the flow (e.g. tap SPACE past a drifted OLDIES wait)."""

    STD = (0x39, 0x48, 0x50, 0x4D, 0x4B, 0x02, 0x03)   # fire, up, down, right, left, '1', '2' (DC1 sources)

    def __init__(self, playback):
        self.events = list(playback.events)            # already sorted by (boundary, seq)
        self.i = 0
        self.boundary = 0
        self.held: set[int] = set()

    def step(self) -> None:
        """Advance one native frame: apply every event due at/under the current boundary, then bump it."""
        while self.i < len(self.events) and self.events[self.i].boundary <= self.boundary:
            ev = self.events[self.i]; self.i += 1
            if ev.kind == "scan":
                sc = ev.value & 0xFF
                (self.held.discard if sc & 0x80 else self.held.add)(sc & 0x7F)
        self.boundary += 1

    @property
    def finished(self) -> bool:
        return self.i >= len(self.events)


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
    ap.add_argument("--snapshot", default=None,
                    help="DEBUG: seed gameplay from a savestate dir (memory_1mb.bin) instead of cold-booting")
    ap.add_argument("--play-demo", default=None,
                    help="replay a recorded demo. If DIR/game_tick_demo.bin exists (created once by "
                         "scripts/verify_native_tick_demo.py DIR), the replay is DETERMINISTIC: seeded from the "
                         "oracle's first gameplay tick, per-tick keys injected, gameplay digest checked vs the VM "
                         "every tick. Otherwise falls back to APPROXIMATE scancode replay (cold boot + live keys "
                         "merged; front-end timing drifts).")
    ap.add_argument("--boot-image", default=str(_BOOT_IMAGE),
                    help="the static boot image (the EXE's init memory); built from PRE2.EXE if absent")
    ap.add_argument("--fps", type=int, default=24,
                    help="gameplay tick-rate cap; default ~24Hz (the main loop waits 3 VGA retraces)")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args(argv)

    import numpy as np
    import pygame
    from dos_re.dos import DOSMachine
    from dos_re.input_demo import InputDemoPlayback
    from pre2.checkpoints.common import Pre2HybridGap
    from pre2.native.cold_boot import load_boot_image, native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.input import init_keyboard_input, set_key
    from pre2.native.render import native_load_level_palette
    from pre2.native.runtime import native_frame_step, native_level_reveal
    from pre2.native.state import NativeGameState
    from sdl_view import front_end_scene_to_rgb, render_planar_rgb_from_planes

    gr = str(ROOT / "assets")
    boot_image = _ensure_boot_image(Path(args.boot_image))
    demo = DemoInput(InputDemoPlayback.load(args.play_demo)) if args.play_demo else None
    if demo is not None:
        print(f"--play-demo: replaying {len(demo.events)} input events (hands-free; live keys merged, ESC quits)")

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

    def drive_input(state):
        """Write DC1's key table from the demo (if replaying) merged with live host keys, then advance the demo
        by one frame. Numpad + arrows = move, SPACE = fire/jump, 1/2 = mode-select. Shared by front-end + game."""
        if demo is not None:
            demo.step()
        k = pygame.key.get_pressed()
        held = set(demo.held) if demo is not None else set()
        if k[pygame.K_SPACE]:
            held.add(0x39)
        if k[pygame.K_UP] or k[pygame.K_KP8]:
            held.add(0x48)
        if k[pygame.K_DOWN] or k[pygame.K_KP2]:
            held.add(0x50)
        if k[pygame.K_RIGHT] or k[pygame.K_KP6]:
            held.add(0x4D)
        if k[pygame.K_LEFT] or k[pygame.K_KP4]:
            held.add(0x4B)
        if k[pygame.K_1] or k[pygame.K_KP1]:
            held.add(0x02)
        if k[pygame.K_2]:
            held.add(0x03)
        for sc in set(DemoInput.STD) | held:
            set_key(state, sc, sc in held)

    # ---- audio: the recovered ENHANCED player (VM-free), driven by the native frame's audio commands ----
    native_audio = None
    try:
        from sdl_view import SdlEnhancedAudio
        from pre2.native.audio import NativeAudio
        native_audio = NativeAudio(SdlEnhancedAudio(pygame, gr, {}).post, gr)
    except Exception as e:                                          # noqa: BLE001 — no audio device -> run silent
        print(f"  (audio disabled: {type(e).__name__}: {str(e)[:60]})")

    def reveal_level(state, dos):
        """Curtain the freshly-loaded level in (the VM's 3054 center-out level-start reveal) instead of it
        appearing instantly. Driven once at every level start (cold boot + between-levels)."""
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        for planes, page in native_level_reveal(state, dos, disp, game_root=gr):
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _FRONT_END_FPS,
                    "PRE2 VM-less — level start")
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return

    def between_levels(state, dos):
        """The between-levels flow (the VM's 4F65 -> BRAVO tally -> CARTE world map -> next-level load): advance
        + load the next level (byte-exact), then drive the recovered CARTE scene with the 'you are here' marker
        at the NEW level (the VM advances [0x2D8A] before the carte too). The exit-iris + the BRAVO tally scene
        are not yet recovered (the next front-end island) — announced, never silently skipped."""
        from pre2.native.front_end import _native_carte
        from pre2.native.level_state import native_level_end
        print("  level complete -> carte (exit-iris + BRAVO tally scene not yet native — the next island)")
        native_level_end(state, game_root=gr)
        for scene in _native_carte(state, dos, gr):                # fire (press after release) advances
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — world map")
            pump()
            drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # CARTE.TRK
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)                      # restore the level palette after the carte DAC
        reveal_level(state, dos)                                    # 3054 center-out curtain into the next level

    def gameplay_loop(state, dos):
        """Run the recovered gameplay VM-less: host input -> native_frame_step -> present, until a gap."""
        print("Gameplay — SPACE = fire/jump, arrows/numpad = move, ESC = quit. (VM-less native gameplay)")
        from pre2.checkpoints.common import Pre2LevelEndTransition
        n = 0
        while ref["running"]:
            pump()
            drive_input(state)
            disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
            try:
                for planes, page in native_frame_step(state, dos, disp, game_root=gr):
                    rgb = render_planar_rgb_from_planes(planes, page, dos.vga_palette)
                    n += 1
                    present(rgb, args.fps, None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")
                    pump()
                    if not ref["running"]:
                        break
            except Pre2LevelEndTransition:
                between_levels(state, dos)                          # tally/carte flow, then the next level
            except Exception as e:                                  # noqa: BLE001 — hold on an unrecovered gap
                hold_last(f"gameplay gap: {type(e).__name__}: {str(e)[:80]}")
                return
            if native_audio is not None:
                native_audio.poll(state)

    if args.play_demo:
        tick_path = Path(args.play_demo) / "game_tick_demo.bin"
        if tick_path.exists():
            # ---- DETERMINISTIC tick replay: seed + per-tick keys + per-tick digest from the VM oracle ----
            # (produced by scripts/verify_native_tick_demo.py; keyed to GAME TICKS, so it replays identically
            # in every mode. Live keys are IGNORED during the replay — determinism first; ESC still quits.)
            from pre2.checkpoints.common import Pre2LevelEndTransition
            from pre2.native.game_tick_demo import GameTickDemo, _inject, gameplay_digest
            gtd = GameTickDemo.load(tick_path)
            print(f"tick replay: {gtd.n_ticks} game ticks (deterministic; digest-checked vs the VM oracle)")
            state = NativeGameState(bytearray(gtd.seed))           # the VM's memory at the first gameplay tick
            dos = DOSMachine(gr)
            native_load_level_palette(state, dos)
            div = None
            i = 0
            while ref["running"] and i < gtd.n_ticks:
                pump()
                _inject(state, gtd.keys[i])
                disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
                try:
                    for planes, page in native_frame_step(state, dos, disp, game_root=gr):
                        present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), args.fps,
                                f"PRE2 VM-less — tick replay {i}/{gtd.n_ticks}" if i % 20 == 0 else None)
                        pump()
                        if not ref["running"]:
                            break
                except Pre2LevelEndTransition:
                    print(f"  tick replay: LEVEL END at tick {i} — the compare ends here; continuing live")
                    between_levels(state, dos)
                    break
                except Exception as e:                             # noqa: BLE001
                    hold_last(f"tick replay gap at tick {i}: {type(e).__name__}: {str(e)[:70]}")
                    pygame.quit()
                    return 0
                if div is None and gameplay_digest(state.data[DS:DS + 0x10000]) != gtd.digests[i]:
                    div = i
                    print(f"  tick replay DIVERGENCE at tick {i} (gameplay digest mismatch) — continuing")
                if native_audio is not None:
                    native_audio.poll(state)
                i += 1
            if div is None and i:
                print(f"  tick replay: {i} ticks reproduced byte-identically (digest matched every tick)")
            if ref["running"]:
                gameplay_loop(state, dos)                          # hand over to live play
            pygame.quit()
            return 0
        print(f"(no {tick_path.name} in the demo — approximate input replay; run "
              f"scripts/verify_native_tick_demo.py {args.play_demo} once to make it deterministic)")

    if args.from_level is not None:
        # ---- DEBUG path: jump straight into a level for gameplay testing (no front-end) ----
        print(f"--from-level {args.from_level}: booting LEVEL{args.from_level + 1} directly (VM-less, no front-end)...")
        state = native_cold_boot(gr, boot_image, level=args.from_level)
        dos = DOSMachine(gr)
        native_load_level_palette(state, dos)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)
        pygame.quit()
        return 0

    if args.snapshot is not None:
        # ---- DEBUG path: seed gameplay from a savestate's raw memory (VM-less), then run native forward ----
        state = NativeGameState(bytearray((Path(args.snapshot) / "memory_1mb.bin").read_bytes()))
        lvl = state.data[DS + 0x2D8A]
        frame_ctr = state.data[DS + 0x6BD5] | (state.data[DS + 0x6BD6] << 8)
        player_zero = not any(state.data[DS + 0x4F1C:DS + 0x4F20])   # X+Y both zero
        if frame_ctr == 0 and player_zero:
            # A savestate taken DURING a level-load / transition (F12 mid-curtain): the gameplay DGROUP is not
            # populated yet (player/objects/camera/frame-counter all zero, ip parked in the loader's retrace
            # wait). Native has no "resume a half-loaded level" path, so seed a CLEAN LEVEL{lvl+1} instead.
            print(f"--snapshot: DGROUP is PRE-GAMEPLAY (level {lvl + 1} mid-load — player/objects/frame-counter "
                  f"all zero). Native can't resume a half-loaded level; booting LEVEL{lvl + 1} fresh instead.")
            state = native_cold_boot(gr, boot_image, level=lvl)
        else:
            print(f"--snapshot: seeding LEVEL{lvl + 1} gameplay from the savestate (VM-less)...")
        init_keyboard_input(state)
        dos = DOSMachine(gr)
        native_load_level_palette(state, dos)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
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
            # the OLDIES scene-wait (0bbe) reads fire; the mode-select toggles BEGINNER<->EXPERT on UP/DOWN and
            # the carte pans on the arrows; '1'/'2' start / password. drive_input feeds all of these (demo + live).
            drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # front-end music (PRESENTA title song, menu, carte)
            if not ref["running"]:
                break
        reached_gameplay = ref["running"]                          # the generator finished -> a level started
    except Pre2HybridGap as e:
        hold_last(f"front-end reached a not-yet-recovered gap: {str(e)[:110]}")
    except Exception as e:                                         # noqa: BLE001
        hold_last(f"front-end error: {type(e).__name__}: {str(e)[:90]}")

    if reached_gameplay and ref["running"]:
        native_load_level_palette(state, dos)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
