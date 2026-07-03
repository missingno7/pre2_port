"""Play Prehistorik 2 with the VM-LESS native core — COLD BOOT from the OLDIES screen through the whole flow.

The standalone runner, with NO emulator anywhere: from the BOOT CONSTANTS (pre2/native/boot_data.py — the game's
initialized data segment, no EXE needed) + the GOG ``*.SQZ`` assets, it drives the recovered FRONT-END flow (OLDIES credits ->
TITUS title -> PREHISTORIK-2 title -> menu -> world map -> level) and then the recovered GAMEPLAY — no x86 is
interpreted and ``PRE2.EXE`` is never executed at runtime. This is the VM-less counterpart of ``play.py --view``:
it starts at the very first screen, exactly like the real game, and runs forward until it hits a not-yet-recovered
gap (where it stops and reports, rather than silently faking anything).

    python scripts/play_native.py                  # full cold start: OLDIES -> titles -> ... (the real boot)
    python scripts/play_native.py --from-level 0    # DEBUG: skip the front-end, drop straight into LEVEL1 gameplay
    python scripts/play_native.py --fps 30          # gameplay tick rate (front-end runs at its native 70Hz)

Controls: SPACE = advance the OLDIES screen / fire+jump in game; arrow keys / numpad = move; ESC = quit.

THE BOOT STATE is pure constants (pre2/native/boot_data.py, generated once by pre2/probes/extract_boot_data.py —
the VM's only remaining, workbench-side role). No PRE2.EXE and no boot image at runtime: copy the package +
the game data anywhere and run.

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
_FRONT_END_FPS = 70           # the front-end runs at the VGA retrace rate (its FrontEndScene frames are per-retrace)
_TRANSITION_FPS = 30          # curtains/fades: the VM's 3054/30C6 are vsync-paced sub-frame effects that span
#                               ~20 retraces (~0.34s); presenting the ~11 reveal steps at 70Hz was ~2x too fast.


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
    ap.add_argument("--game-root", default=str(ROOT / "assets"),
                    help="folder with the game data files (*.SQZ/*.TRK — e.g. the GOG Prehistorik 2 install "
                         "dir); default: the repo's assets/")
    ap.add_argument("--fps", type=int, default=24,
                    help="gameplay tick-rate cap; default ~24Hz (the main loop waits 3 VGA retraces)")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args(argv)

    import numpy as np
    import pygame
    from pre2.native.vga import NativeVGA
    from pre2.gaps import Pre2HybridGap
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.input import init_keyboard_input, set_key
    from pre2.native.render import native_load_level_palette
    from pre2.native.runtime import (native_exit_anim, native_frame_step, native_iris_close,
                                      native_level_reveal)
    from pre2.native.state import NativeGameState
    from sdl_view import front_end_scene_to_rgb, render_planar_rgb_from_planes

    gr = str(Path(args.game_root))
    if not (Path(gr) / "SPRITES.SQZ").exists():
        raise SystemExit(f"--game-root {gr}: no SPRITES.SQZ here — point it at the Prehistorik 2 data folder")
    demo = None
    if args.play_demo and not (Path(args.play_demo) / "game_tick_demo.bin").exists():
        # APPROXIMATE scancode fallback only — the deterministic tick replay below doesn't need the input demo.
        # Lazy import: dos_re.input_demo is VM-side plumbing the deployed standalone doesn't ship (fails loud here).
        from dos_re.input_demo import InputDemoPlayback
        demo = DemoInput(InputDemoPlayback.load(args.play_demo))
        print(f"--play-demo: replaying {len(demo.events)} input events (hands-free; live keys merged, ESC quits)")

    pygame.init()
    view = {"screen": pygame.display.set_mode((320 * args.scale, 200 * args.scale), pygame.RESIZABLE)}
    clock = pygame.time.Clock()
    ref = {"running": True, "last": None, "last_scan": 0}
    # ENTER-CODE (password screen): host hex key -> DOS make code. The game maps the DOS make code to a hex char
    # via its own [0xB068] table, so we must feed the make code of the PHYSICAL key position — like the original,
    # which reads raw scancodes. Key by SDL physical scancode (ev.scancode), NOT the keysym (ev.key): the keysym is
    # LAYOUT-dependent, so on a non-US keyboard (e.g. Czech, where the number row is ě š č …) the digit keysyms
    # never match and the code can't be typed. The physical position is layout-independent.
    _SDL_HEX = {30: 0x02, 31: 0x03, 32: 0x04, 33: 0x05, 34: 0x06, 35: 0x07, 36: 0x08, 37: 0x09, 38: 0x0A,   # 1..9
                39: 0x0B,                                                                                    # 0
                4: 0x1E, 5: 0x30, 6: 0x2E, 7: 0x20, 8: 0x12, 9: 0x21,                                        # A..F
                89: 0x02, 90: 0x03, 91: 0x04, 92: 0x05, 93: 0x06, 94: 0x07, 95: 0x08, 96: 0x09, 97: 0x0A,    # KP1..9
                98: 0x0B}                                                                                    # KP0
    _KEYSYM_HEX = {pygame.K_0: 0x0B, pygame.K_1: 0x02, pygame.K_2: 0x03, pygame.K_3: 0x04, pygame.K_4: 0x05,
                   pygame.K_5: 0x06, pygame.K_6: 0x07, pygame.K_7: 0x08, pygame.K_8: 0x09, pygame.K_9: 0x0A,
                   pygame.K_a: 0x1E, pygame.K_b: 0x30, pygame.K_c: 0x2E, pygame.K_d: 0x20, pygame.K_e: 0x12,
                   pygame.K_f: 0x21}

    def pump():
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                ref["running"] = False
            elif ev.type == pygame.VIDEORESIZE:                    # user dragged the window edge -> rebind + rescale
                view["screen"] = pygame.display.set_mode((max(160, ev.w), max(100, ev.h)), pygame.RESIZABLE)
            elif ev.type == pygame.KEYDOWN:                        # latch the hex make code typed THIS frame
                sc = _SDL_HEX.get(getattr(ev, "scancode", -1)) or _KEYSYM_HEX.get(ev.key)   # physical, keysym fallback
                if sc:
                    ref["last_scan"] = sc

    def present(rgb, fps, caption=None):
        arr = np.asarray(rgb, np.uint8)
        fh, fw = arr.shape[:2]                                    # the frame's OWN size (320x200, or 640x480 for
        surf = pygame.surfarray.make_surface(arr.swapaxes(0, 1))  # the 12h creators screen) — fit it aspect-correct
        screen = view["screen"]
        sw, sh = screen.get_size()
        f = min(sw / fw, sh / fh)                                 # fit THIS frame, PRESERVING aspect ratio
        tw, th = max(1, int(fw * f)), max(1, int(fh * f))
        screen.fill((0, 0, 0))                                    # letterbox the unused margin
        screen.blit(pygame.transform.scale(surf, (tw, th)), ((sw - tw) // 2, (sh - th) // 2))
        pygame.display.flip()
        clock.tick(fps)
        if caption:
            pygame.display.set_caption(caption)
        ref["last"] = rgb

    def dump_gap_snapshot(state, msg: str) -> str | None:
        """Write the CURRENT native state as a repro snapshot the workbench loads directly:
        ``<dir>/memory_1mb.bin`` (the full 1.25 MB image — ``--snapshot <dir>`` re-seeds from it, and every
        probe/oracle does ``NativeGameState(bytearray(read_bytes()))``) + ``state.json`` (the gap message +
        the key game state for triage). Frozen exe -> next to the game data (discoverable); repo -> artifacts/."""
        import datetime
        import json
        try:
            base = Path(gr) if getattr(sys, "frozen", False) else ROOT / "artifacts"
            out = base / f"native_gap_{datetime.datetime.now():%Y%m%d_%H%M%S}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "memory_1mb.bin").write_bytes(bytes(state.data))
            d = state.data
            (out / "state.json").write_text(json.dumps({
                "kind": "native_gap",
                "error": msg,
                "level_0x2d8a": d[DS + 0x2D8A],
                "lives_0x27d8": d[DS + 0x27D8],
                "frame_0x6bd5": d[DS + 0x6BD5] | (d[DS + 0x6BD6] << 8),
                "player_x": d[DS + 0x4F1C] | (d[DS + 0x4F1D] << 8),
                "player_y": d[DS + 0x4F1E] | (d[DS + 0x4F1F] << 8),
                "scale_0x6be2": d[DS + 0x6BE2] | (d[DS + 0x6BE3] << 8),
            }, indent=1))
            print(f"  gap snapshot written: {out}")
            print(f"  repro: python scripts/play_native.py --snapshot \"{out}\"")
            return str(out)
        except Exception as e:                                  # noqa: BLE001 — never mask the original gap
            print(f"  (gap snapshot failed: {type(e).__name__}: {e})")
            return None

    def hold_last(msg, state=None):
        """An unrecovered gap (or a finished run): dump a repro snapshot (when the game state is passed),
        print once, hold the last frame until the user quits."""
        if state is not None:
            dump_gap_snapshot(state, msg)
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
        # ENTER-CODE: drive the [0x2874] scancode latch DC1's 99BE reads from the hex key typed THIS frame (0 if
        # none). A per-frame latch (not a persistent queue) is deliberate: the '1'/'2' the player presses to REACH
        # the password screen must NOT leak into the code (they are hex chars too) — writing 0 every idle frame
        # clears any menu keystroke before the screen reads it. The password accumulator maps it via [0xB068].
        latch = ref["last_scan"]
        if state.data[(DS + 0x2879) & 0xFFFFF] == 1 and held:   # ATTRACT demo: ANY key press ends it (DC1 0DD6
            latch = latch or next(iter(held))                   # sets [0x6BE5] on a pending [0x2874]) -> back to menu
        state.data[(DS + 0x2874) & 0xFFFFF] = latch
        ref["last_scan"] = 0

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
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                    "PRE2 VM-less — level start")
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return

    def between_levels(state, dos):
        """The between-levels flow (the VM's 4F65 -> BRAVO tally -> CARTE world map -> next-level load): show the
        level-end TALLY (SCORE / LEVEL COMPLETED %), advance + load the next level (byte-exact), then drive the
        recovered CARTE scene with the 'you are here' marker at the NEW level (the VM advances [0x2D8A] before the
        carte too). The full 4CCB exit-anim cutscene (iris + player walk + food-throw count-up) is deferred; the
        tally TEXT is shown."""
        from pre2.native.audio import native_load_song
        from pre2.native.front_end import _native_carte
        from pre2.native.level_state import level_end_takes_tally, native_level_end

        # [asm 4C69] the level-end dispatch (level_end_takes_tally): a warp INTO a bonus level (4C8F) and a bonus
        # level's own end (4CC1) do `call 30C6` (the vertical close-curtain — the same visual as the cave
        # transition) + `jmp 4F65` (plain reload): NO exit anim, NO tally, NO carte.
        mode = state.data[DS + 0x6BE6]
        level = state.data[DS + 0x2D8A]
        if not level_end_takes_tally(mode, level):                 # [asm 4c93 / 4cc1] the curtain (cave-style) exit
            from pre2.native.audio import native_level_song_name
            from pre2.native.render import native_render, native_sync_render_state
            from pre2.native.runtime import _vfade_frame
            print("  level exit -> BONUS-warp curtain (30C6 close, no tally) -> next level")
            disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
            native_sync_render_state(state)
            base_planes, base_page = native_render(state, dos, disp, game_root=gr, force_gameplay=True)
            for k in range(1, 10):                                 # [asm 30C6] 9-step vertical fade to black
                planes, page = _vfade_frame(base_planes, base_page, k)
                present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                        "PRE2 VM-less — bonus warp")
                pump()
                if not ref["running"]:
                    return
            native_level_end(state, game_root=gr)                  # [asm 4f65] the warp-table level switch + load
            native_load_level_palette(state, dos)
            native_load_song(state, native_level_song_name(state), gr)
            reveal_level(state, dos)                               # 3054 center-out curtain into the new level
            return

        print("  level complete -> IRIS close -> TALLY -> carte (the 4CCB walk/throw count-up is deferred)")
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        for planes, page in native_iris_close(state, dos, disp, game_root=gr):   # 316F circle-close on the player
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _FRONT_END_FPS,
                    "PRE2 VM-less — level complete")
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        try:
            native_load_song(state, "BRAVO.TRK", gr)               # the tally jingle
        except Exception:                                          # noqa: BLE001 — no audio -> silent tally
            pass
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        for planes, page in native_exit_anim(state, dos, disp, game_root=gr):   # walk-in + food + count-up + walk-off
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                    "PRE2 VM-less — LEVEL COMPLETED")   # a main-loop (~23Hz) animation, NOT a 70Hz retrace scene
            pump()
            drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
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
        from pre2.native.audio import native_level_song_name
        native_load_song(state, native_level_song_name(state), gr)  # [asm 01B7] carte song -> the level song
        reveal_level(state, dos)                                    # 3054 center-out curtain into the next level

    def game_over_restart(state, dos):
        """[asm 5063 -> main 011C] The real game-over flow: the GAME OVER scene (9B23 — the GAMEOVER.SQZ
        diorama with the bouncing letters, the crying tableau + circling birds, BOULA.TRK, until fire or the
        ~9 s timeout, then the DAC fade), then main re-enters the front-end at the press-1/2 MENU (8e45) ->
        mode-select map -> carte -> the LEVEL loader with the FRESH-start block (lives reset). Recovered:
        native_gameover_scene (setup+tick byte-exact vs the ASM, 60-frame lockstep) + native_menu_flow (the
        same generator the cold boot runs from the menu on)."""
        from pre2.native.audio import native_load_song
        from pre2.native.front_end import native_menu_flow
        from pre2.native.gameover_scene import native_gameover_scene
        print("  GAME OVER -> the 9B23 scene -> menu -> carte -> restart")
        native_load_song(state, "BOULA.TRK", gr)                   # [asm 5063: 02CC ax=0x11] the game-over song
        for planes, page in native_gameover_scene(state, dos, gr):  # [asm 9B23] the scene (fire/timeout exits)
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _FRONT_END_FPS,
                    "PRE2 VM-less — GAME OVER")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # BOULA.TRK
            if not ref["running"]:
                return
        for scene in native_menu_flow(state, dos, gr):             # [main 011C] menu -> map -> carte -> loader
            fps = args.fps if state.data[DS + 0x2879] == 1 else _FRONT_END_FPS
            present(front_end_scene_to_rgb(scene), fps, "PRE2 VM-less — restart")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)                      # the level palette after the carte DAC
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level

    def the_end_restart(state, dos):
        """[asm 5034 -> main 0x12f] THE END: the player cleared the final level 0xE ([0x6be5]==0xFF). Show the
        THEEND.SQZ screen (FINAL.TRK, fade-in, wait-for-fire, fade-out), then re-enter the front-end MENU (like
        the game-over restart) -> map -> carte -> the LEVEL loader = level 1 started again."""
        from pre2.native.audio import native_load_song
        from pre2.native.front_end import native_menu_flow, native_the_end
        print("  THE END -> THEEND.SQZ screen -> menu -> restart at level 1")
        try:
            native_load_song(state, "FINAL.TRK", gr)               # [asm 5034 region] the ending song
        except Exception:                                          # noqa: BLE001 — no audio -> silent ending
            pass
        for scene in native_the_end(state, gr):                    # [asm 5034] the THE END screen (fire exits)
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — THE END")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # FINAL.TRK
            if not ref["running"]:
                return
        for sel in (0x6BE4, 0x6BE5, 0x6BE6):                       # clear the death/end selectors before the menu
            state.data[DS + sel] = 0
        for scene in native_menu_flow(state, dos, gr):             # [main 0x12f] menu -> map -> carte -> loader (L1)
            fps = args.fps if state.data[DS + 0x2879] == 1 else _FRONT_END_FPS
            present(front_end_scene_to_rgb(scene), fps, "PRE2 VM-less — restart")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)
        reveal_level(state, dos)

    def gameplay_loop(state, dos):
        """Run the recovered gameplay VM-less: host input -> native_frame_step -> present, until a gap."""
        print("Gameplay — SPACE = fire/jump, arrows/numpad = move, ESC = quit. (VM-less native gameplay)")
        from pre2.gaps import Pre2GameComplete, Pre2GameOverTransition, Pre2LevelEndTransition
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
                    if native_audio is not None:
                        native_audio.poll(state)               # PER FRAME: a transition (death fly-off) yields
                        #   dozens of frames in ONE step — its queued sfx (the death SCREAM at the bounce start,
                        #   509d/50a6) must sound AT that frame, not after the whole animation
                    if not ref["running"]:
                        break
            except Pre2LevelEndTransition:
                between_levels(state, dos)                          # tally/carte flow, then the next level
            except Pre2GameComplete:
                the_end_restart(state, dos)                        # THE END screen; then menu -> restart at level 1
            except Pre2GameOverTransition:
                game_over_restart(state, dos)                      # death-bounce shown; restart at level 1
            except Exception as e:                                  # noqa: BLE001 — hold on an unrecovered gap
                hold_last(f"gameplay gap: {type(e).__name__}: {str(e)[:80]}", state)
                return
            if native_audio is not None:
                native_audio.poll(state)

    if args.play_demo:
        tick_path = Path(args.play_demo) / "game_tick_demo.bin"
        if tick_path.exists():
            # ---- DETERMINISTIC tick replay: seed + per-tick keys + per-tick digest from the VM oracle ----
            # (produced by scripts/verify_native_tick_demo.py; keyed to GAME TICKS, so it replays identically
            # in every mode. Live keys are IGNORED during the replay — determinism first; ESC still quits.)
            from pre2.gaps import Pre2GameComplete, Pre2GameOverTransition, Pre2LevelEndTransition
            from pre2.native.game_tick_demo import GameTickDemo, _inject, gameplay_digest
            gtd = GameTickDemo.load(tick_path)
            print(f"tick replay: {gtd.n_ticks} game ticks (deterministic; digest-checked vs the VM oracle)")
            state = NativeGameState(bytearray(gtd.seed))           # the VM's memory at the first gameplay tick
            dos = NativeVGA()
            native_load_level_palette(state, dos)
            div = None
            i = 0
            while ref["running"] and i < gtd.n_ticks:
                pump()
                _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
                disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
                try:
                    for planes, page in native_frame_step(state, dos, disp, game_root=gr):
                        present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), args.fps,
                                f"PRE2 VM-less — tick replay {i}/{gtd.n_ticks}" if i % 20 == 0 else None)
                        pump()
                        if native_audio is not None:
                            native_audio.poll(state)           # per frame (death fly-off sfx timing)
                        if not ref["running"]:
                            break
                except Pre2LevelEndTransition:
                    print(f"  tick replay: LEVEL END at tick {i} — the compare ends here; continuing live")
                    between_levels(state, dos)
                    break
                except Pre2GameOverTransition:
                    print(f"  tick replay: GAME OVER at tick {i} — the compare ends here; restarting live")
                    game_over_restart(state, dos)
                    break
                except Pre2GameComplete:
                    print(f"  tick replay: THE END at tick {i} — the game is finished")
                    the_end_restart(state, dos)
                    break
                except Exception as e:                             # noqa: BLE001
                    hold_last(f"tick replay gap at tick {i}: {type(e).__name__}: {str(e)[:70]}", state)
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
        state = native_cold_boot(gr, level=args.from_level)
        dos = NativeVGA()
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
            state = native_cold_boot(gr, level=lvl)
        else:
            print(f"--snapshot: seeding LEVEL{lvl + 1} gameplay from the savestate (VM-less)...")
        init_keyboard_input(state)
        dos = NativeVGA()
        native_load_level_palette(state, dos)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)
        pygame.quit()
        return 0

    # ---- the real cold start: OLDIES -> titles -> menu -> map -> level, all VM-less ----
    print("Cold boot from the OLDIES screen (VM-less). SPACE to advance, ESC to quit...")
    state = NativeGameState(build_boot_memory())      # the boot CONSTANTS (no EXE, no boot image)
    init_keyboard_input(state)                                     # the boot joystick-detect outcome (DC1 input)
    dos = NativeVGA()
    reached_gameplay = False
    try:
        for scene in native_front_end(state, dos, 0, game_root=gr):
            # front-end scenes are per-retrace (70Hz), but the ATTRACT demo ([0x2879]=1) is GAMEPLAY and must run at
            # the game rate (args.fps ~24Hz) or it plays ~3x too fast.
            fps = args.fps if state.data[DS + 0x2879] == 1 else _FRONT_END_FPS
            present(front_end_scene_to_rgb(scene), fps, "PRE2 VM-less — cold boot (front-end)")
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
        hold_last(f"front-end reached a not-yet-recovered gap: {str(e)[:110]}", state)
    except Exception as e:                                         # noqa: BLE001
        hold_last(f"front-end error: {type(e).__name__}: {str(e)[:90]}", state)

    if reached_gameplay and ref["running"]:
        native_load_level_palette(state, dos)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
