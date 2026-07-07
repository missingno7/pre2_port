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

STATUS: plays the full flow VM-less — OLDIES + the two title screens + attract animation + the "press 1/2" menu +
password entry + the mode-select world-map + the CARTE scroll-in, then GAMEPLAY: the whole per-frame loop (player
FSM/movement/collision, the object + second passes, terrain/effects, the 88D7 combat pass, and the 4C69 level
state machine — death / respawn / checkpoint / level-end tally / game-over / game-complete), with digital SFX and
per-level music, level transitions, and the LEVELG snow/wind. Verified byte-exact vs the pure-ASM oracle tick by
tick. ``--from-level`` boots a level directly for testing. Known residuals are small and fail loud (a few rare
edge-case paths, e.g. the game-over-via-respawn tail; the level-end count-up *cutscene* animation is deferred).
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
VIEWPORT_H = 176              # gameplay viewport rows (the HUD strip below never scrolls / fades)
_VFADE_MID = 88               # the 30C6 vertical fade's two bands meet here (fully closed)
# Smooth-transition durations in wall-clock SECONDS (present-time driven -> frame-rate-independent).
_CAM_TAU = 0.07              # smooth-camera easing time constant (s): larger = softer / laggier follow
_REVEAL_S = 0.45             # level-start / checkpoint / cave center-out curtain reveal
_IRIS_S = 1.0                # level-end circular iris close
_FADE_S = 0.35               # cave / death vertical fade-to-black
_CAVE_BLACK_S = 0.12         # the brief black hold while the camera pans behind the fade
_TRANSITION_FPS = 30          # curtains/fades: the VM's 3054/30C6 are vsync-paced sub-frame effects that span
#                               ~20 retraces (~0.34s); presenting the ~11 reveal steps at 70Hz was ~2x too fast.
TICK_HZ = 70.0 / 3.0          # the game's own tick rate: the 1C6F frame limiter waits 3 PIT/retrace periods per
#                               main-loop tick (70Hz VGA / 3) — the faithful gameplay pacing (~23.33Hz). The old
#                               default of 24 was a ~3% -fast approximation of this.


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
    # Frozen exe: look for the game data NEXT TO the .exe (drop it into the GOG folder and run). Source run:
    # the repo's assets/. (ROOT is the PyInstaller temp extraction dir when frozen, so it can't hold the data.)
    _default_game_root = str(Path(sys.executable).parent) if getattr(sys, "frozen", False) else str(ROOT / "assets")
    ap.add_argument("--game-root", default=_default_game_root,
                    help="folder with the game data files (*.SQZ/*.TRK — e.g. the GOG Prehistorik 2 install "
                         "dir); default: next to the .exe (frozen) or the repo's assets/ (source)")
    ap.add_argument("--fps", type=float, default=None,
                    help="gameplay tick-rate cap; default = the faithful 70/3 Hz (~23.33 — the original's "
                         "main loop waits 3 VGA retraces per tick)")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--debug", action="store_true",
                    help="show the Develop tab in the F10 overlay menu (level select, god mode — cheats; "
                         "hidden from the end-user product by default)")
    args = ap.parse_args(argv)
    if args.fps is None:
        args.fps = TICK_HZ                              # faithful pacing unless the user overrides

    import numpy as np
    import pygame
    from pre2.native.vga import NativeVGA
    from pre2.gaps import Pre2HybridGap
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.input import init_keyboard_input, set_key
    from pre2.native.render import native_load_level_palette
    from pre2.native.runtime import (native_exit_anim, native_frame_step, native_frame_step_tagged,
                                      native_iris_close, native_level_reveal)
    from pre2.native.state import NativeGameState
    from sdl_view import front_end_scene_to_rgb, render_planar_rgb_from_planes

    gr = str(Path(args.game_root))
    if not (Path(gr) / "SPRITES.SQZ").exists():
        raise SystemExit(f"--game-root {gr}: no SPRITES.SQZ here — point it at the Prehistorik 2 data folder")
    demo = None
    if args.play_demo and not (Path(args.play_demo) / "game_tick_demo.bin").exists():
        # APPROXIMATE scancode fallback only — the deterministic tick replay below doesn't need the input demo
        # (it has its own exact per-tick keys, gtd.keys, covering the WHOLE recording — see the loop below).
        # Lazy import: dos_re.input_demo is VM-side plumbing the deployed standalone doesn't ship (fails loud here).
        from dos_re.input_demo import InputDemoPlayback
        demo = DemoInput(InputDemoPlayback.load(args.play_demo))
        print(f"--play-demo: replaying {len(demo.events)} input events (hands-free; live keys merged, ESC quits)")

    # DPI awareness BEFORE any window exists: on Windows with display scaling (e.g. 150%) an un-aware process
    # gets the LOGICAL desktop size, so a borderless-fullscreen window doesn't cover the physical screen and
    # its (0,0) placement drifts. Make the process per-monitor DPI-aware so get_desktop_sizes() = real pixels.
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
        except Exception:                                    # noqa: BLE001 — older Windows
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:                                # noqa: BLE001
                pass
    import os as _os
    _os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")   # SDL's own DPI path (>= 2.24)
    _os.environ.setdefault("SDL_HINT_RENDER_SCALE_QUALITY", "0")          # crisp nearest-neighbour GPU upscale
    pygame.init()
    from display import Display
    # GPU-accelerated present (SDL2 renderer): uploads the small game frame and lets the GPU scale it to the
    # window, so fps no longer collapses as the window grows (the old software path scaled + flipped the whole
    # window surface every frame). Falls back to a software surface where the renderer is unavailable.
    disp = Display((320 * args.scale, 200 * args.scale))
    view = {"win_size": (320 * args.scale, 200 * args.scale)}     # remembered windowed size (for exit-fullscreen)
    clock = pygame.time.Clock()

    # ---- GAMEPAD (auto-detected) --------------------------------------------------------------------------
    # PRE2's own joystick was analog-READ (RC-timed game port 0x201) but immediately DIGITIZED to the same six
    # on/off flags as the keyboard — up/down/left/right + fire — and OR'd into one shared flag set; the engine
    # has a single fixed walk speed, so analog magnitude is discarded downstream. So there's nothing to gain from
    # the (unwired, gated-absent) emulated game port: we feed a host controller straight into the key table just
    # like the keyboard. Left stick (dead-zoned) + D-pad -> directions; bottom face button (or stick/D-pad up) ->
    # JUMP (the UP flag [0x27EA]); other face buttons -> ATTACK (fire); Start -> begin the game (the '1' key).
    pygame.joystick.init()
    pads = {}                                    # instance_id -> Joystick (supports hot-plug)
    _PAD_DEADZONE = 0.5                           # analog stick -> 8-way threshold (firm; the game is 8-way anyway)
    _PAD_JUMP_BTNS = (0,)                         # A / Cross            -> jump (UP)
    _PAD_FIRE_BTNS = (1, 2, 3)                    # B/X/Y / Circle/Sq/Tri -> attack (fire); also advances scenes
    _PAD_START_BTNS = (6, 7, 9)                   # Back/Start/Menu (index varies by SDL) -> '1' = start the game

    def _open_pad(index):
        try:
            js = pygame.joystick.Joystick(index); js.init()
            pads[js.get_instance_id()] = js
            print(f"gamepad connected: {js.get_name()} "
                  f"({js.get_numaxes()} axes, {js.get_numhats()} hats, {js.get_numbuttons()} buttons)")
        except Exception as e:                    # noqa: BLE001 — a flaky pad must never stop the game
            print(f"(gamepad init failed: {type(e).__name__}: {e})")

    for _i in range(pygame.joystick.get_count()):
        _open_pad(_i)

    def pad_scancodes():
        """The connected controller(s) as DOS scancodes — the SAME flags the keyboard writes, so the game can't
        tell them apart. Left stick (dead-zoned) + D-pad -> directions; face buttons -> jump/attack; Start -> '1'."""
        out = set()
        for js in list(pads.values()):
            try:
                nax = js.get_numaxes()
                x = js.get_axis(0) if nax > 0 else 0.0
                y = js.get_axis(1) if nax > 1 else 0.0
                if x <= -_PAD_DEADZONE: out.add(0x4B)          # left
                elif x >= _PAD_DEADZONE: out.add(0x4D)         # right
                if y <= -_PAD_DEADZONE: out.add(0x48)          # up (SDL -Y) = jump
                elif y >= _PAD_DEADZONE: out.add(0x50)         # down
                if js.get_numhats() > 0:
                    hx, hy = js.get_hat(0)
                    if hx < 0: out.add(0x4B)
                    elif hx > 0: out.add(0x4D)
                    if hy > 0: out.add(0x48)                   # SDL hat +Y is up
                    elif hy < 0: out.add(0x50)
                nb = js.get_numbuttons()
                if any(b < nb and js.get_button(b) for b in _PAD_JUMP_BTNS): out.add(0x48)   # jump = UP
                if any(b < nb and js.get_button(b) for b in _PAD_FIRE_BTNS): out.add(0x39)   # attack = fire
                if any(b < nb and js.get_button(b) for b in _PAD_START_BTNS): out.add(0x02)  # '1' = start / advance menu
            except Exception:                     # noqa: BLE001 — pad unplugged mid-poll -> skip it this frame
                continue
        return out

    def detect_display_hz() -> float:
        """The monitor's REAL current refresh rate. pygame's get_current_refresh_rate is unreliable (often
        reports 60 regardless), so on Windows read the actual mode via GDI VREFRESH first."""
        if sys.platform == "win32":
            try:
                import ctypes
                hdc = ctypes.windll.user32.GetDC(None)
                hz = ctypes.windll.gdi32.GetDeviceCaps(hdc, 116)         # VREFRESH (current display refresh)
                ctypes.windll.user32.ReleaseDC(None, hdc)
                if hz and hz > 1:                                        # 0/1 = "default"; a real panel is >1
                    return float(hz)
            except Exception:                                            # noqa: BLE001
                pass
        try:
            r = float(pygame.display.get_current_refresh_rate())        # pygame >= 2.2 (fallback)
            if r > 0:
                return r
        except Exception:                                                # noqa: BLE001 — older pygame / headless
            pass
        return 60.0                                                      # safe fallback
    # The MONITOR refresh rate — the presentation clock interpolation/smooth transitions present at. The game
    # TICK stays locked at TICK_HZ regardless; only how many presented frames per tick depend on this.
    display_hz = detect_display_hz()
    print(f"display: {display_hz:.0f} Hz (game tick {TICK_HZ:.2f} Hz)")
    ref = {"running": True, "last": None, "last_scan": 0, "p_prev": False, "display_hz": display_hz,
           "menu_request": False, "switch_level": None, "tick_count": 0, "state": None, "snap_request": False,
           "jump_edge": False, "jump_buf": 0}   # RESPONSIVE CONTROLS: pending UP key-down edge + buffered ticks
    #   ref["state"] = the live NativeGameState (set once gameplay starts); ref["snap_request"] = F11 debug dump.

    # RESPONSIVE CONTROLS (experimental): how many game ticks a jump press stays virtually held. The game samples
    # the keyboard once per ~23 Hz tick, so a tap shorter than a tick (or landed a frame early) can be missed;
    # holding it a few ticks makes every tap register + gives a small "jump buffer" (a press just before landing
    # still fires). 4 ticks (~170 ms) is well under a full jump arc, so it can't cause an unintended second jump.
    _JUMP_BUFFER_TICKS = 4

    # --- the end-user settings (the F10 menu edits these; the CLI shrinks to dev flags) -----------------
    import json
    settings_path = Path(gr) / "pre2native_settings.json"
    settings = {"integer_scale": False, "fps_overlay": False, "music": True, "sfx": True, "god": False,
                "stereo_sfx": True,   # ENHANCED: pan effects by where on screen they fire (music is already stereo)
                "interpolation": False, "frame_cap": 0,   # 0 = Display (detected Hz), -1 = Uncapped, else Hz
                "widescreen": False, "fullscreen": False, "true_widescreen": False,
                "smooth_transitions": False, "hud_align": "center",   # widescreen HUD: left / center / right
                "overlay_scale": "auto",   # F10 menu size: "auto" (by window) or 100/150/200/300 (%)
                "widescreen_bg": "mirror",   # widescreen backdrop margins: stretch / mirror / black
                "pixel_aspect": "square",   # "square" (1:1, sharp; keeps the iris a true circle) or "4:3" (CRT
                #                             proportions -> more widescreen margin, but the pixel-circle iris ovals)
                "widescreen_aspect": "auto",   # widescreen target aspect: "auto" (fill the window) or a fixed
                #                                16:9 / 16:10 / 4:3 / 21:9 / 32:9 (letterboxed if the window differs)
                "widescreen_active_zone": False,   # EXPERIMENTAL: activate enemies/objects across the widescreen
                #                                    margins (state-mutating, gameplay-affecting) instead of frozen
                "smooth_camera": False,   # X+Y band-drag presentation camera (experimental)
                "camera_smoothing": 0,   # EXPERIMENTAL "bumping strength": 0=Off..3=High vertical band-drag damp
                "responsive_controls": False}   # EXPERIMENTAL: buffer the jump key so fast taps are never dropped
    try:
        settings.update({k: v for k, v in json.loads(settings_path.read_text()).items() if k in settings})
    except Exception:                                                     # noqa: BLE001 — first run / unreadable
        pass
    settings["god"] = False                                              # a cheat never persists across runs

    def save_settings():
        try:
            persist = {k: v for k, v in settings.items() if k != "god"}
            settings_path.write_text(json.dumps(persist, indent=1))
        except Exception as e:                                            # noqa: BLE001 — read-only game dir
            print(f"(settings not saved: {e})")

    def set_fullscreen(on: bool) -> None:
        """Borderless fullscreen (SDL's own fullscreen-desktop on the GPU renderer — DPI/monitor-correct, instant
        alt-tab, no video-mode switch) <-> the remembered resizable window."""
        settings["fullscreen"] = on
        if on and not view.get("fs"):
            view["win_size"] = disp.get_size()                            # remember the windowed size for the way back
        view["fs"] = on
        disp.set_fullscreen(on, windowed_size=view.get("win_size"))
        ref["display_hz"] = detect_display_hz()                          # the window may now be on another monitor
        save_settings()

    if settings["fullscreen"]:                                            # persisted preference -> apply at boot
        set_fullscreen(True)

    _WIDE_MAX = 272                                       # widescreen margin cap (px/side): fills 32:9 in BOTH
    #                                                        aspect modes (4:3 super-ultrawide wants ~267/side).

    def _pixel_par() -> float:
        """Displayed pixel aspect (height/width): 1.2 for the DOS 4:3 look, 1.0 for square pixels."""
        return 1.2 if settings.get("pixel_aspect") == "4:3" else 1.0

    _WS_ASPECT_RATIOS = {"16:9": 16 / 9, "16:10": 16 / 10, "4:3": 4 / 3, "21:9": 21 / 9, "32:9": 32 / 9}

    def _target_ratio() -> float:
        """The widescreen TARGET display aspect: the live window ('auto') or a fixed pick. 0 if unavailable."""
        wa = settings.get("widescreen_aspect", "auto")
        if wa in _WS_ASPECT_RATIOS:
            return _WS_ASPECT_RATIOS[wa]
        sw, sh = disp.get_size()                          # 'auto' -> fill the actual window
        return sw / sh if sh > 0 else 0.0

    def wide_margin() -> int:
        """The widescreen margin (px each side) needed to reach the TARGET display aspect at the game's displayed
        frame height, capped. In 4:3-pixel mode the frame is 240 units tall (200 * 1.2), so the same aspect fits
        ~3x more margin than square-pixel's 200. 0 when Widescreen is off or the target is <= the game's 4:3 base
        (e.g. widescreen_aspect '4:3' -> no horizontal extension)."""
        if not settings["widescreen"]:
            return 0
        ratio = _target_ratio()
        if ratio <= 0:
            return 0
        eh = 200 * _pixel_par()                           # displayed frame height in px units (240 for 4:3)
        return min(_WIDE_MAX, max(0, (round(eh * ratio) - 320 + 1) // 2))

    # Levels whose GAMEPLAY must render in the plain 4:3 pipeline (widescreen would reveal off-screen tilemap
    # columns / break the fight). LEVEL A (0x09) is the gorilla boss — its alternate faces sit just right of the
    # 320 window and its fight only plays right faithfully (verified: works with widescreen OFF). On it, gameplay
    # uses the faithful stream path (see `enhance_ok`); when Widescreen is on we still give a WIDE HUD via
    # `_widehud_frame` (black gameplay borders + a stretched HUD — "some widescreen feeling").
    _WS_EXCLUDE_LEVELS = {0x09}
    # ROOM levels: the playable area is a 320-wide room/band inside the 256-tile backing map (LEVEL6's tower
    # sections sit side by side; LEVEL F is single-screen), so widescreen margins must NOT reveal the neighbouring
    # columns. These render CENTRED with pure black margins (extract room_mode) — and keep every presentation
    # enhancement (interpolation, smooth transitions, wide HUD); only margin content + the smooth camera are off.
    _WS_ROOM_LEVELS = {0x05, 0x0E}

    def _room_for(state) -> bool:
        """Widescreen ROOM mode for the current level (centred 320, black margins, no margin content)."""
        return state.data[DS + 0x2D8A] in _WS_ROOM_LEVELS

    def _room_locked(state) -> bool:
        """True when the widescreen view must black its margins (room_mode): a whole-room LEVEL (LEVEL6/F), OR a
        FIXED-CAMERA spot — a cave / bonus room the game flags so its camera never scrolls, which is exactly
        where the ultrawide margins would wrongly reveal the tilemap past the room's edge. Uses the game's OWN
        camera-follow gate (native_camera_follow, 1030:5643): the horizontal follow (57A8) is skipped when
        ``[0x6BD9]!=0`` (the whole follow is off — set to the per-room flag by the cave-teleport, [asm 564E]) or
        the ``[0x8166]&2`` horizontal-follow-off mode bit ([asm 5655]). That flag is 0 in every normal
        scrolling-gameplay spot (including standing still), so a fixed camera is authoritatively distinguished
        from a merely-parked one — no heuristic, no false positives, and it catches death-pit rooms a wall-jam
        test never could (the whole point: it's the flag the game itself uses to stop scrolling)."""
        return (state.data[DS + 0x2D8A] in _WS_ROOM_LEVELS
                or state.data[DS + 0x6BD9] != 0
                or (state.data[DS + 0x8166] & 2) != 0)

    def _margin_for(state) -> int:
        """wide_margin(), but forced to 0 on levels that can't be widescreened at all (LEVEL A boss)."""
        if state.data[DS + 0x2D8A] in _WS_EXCLUDE_LEVELS:
            return 0
        return wide_margin()

    from pre2.enhanced.smooth_camera import CROP as _CAM_CROP        # the band-drag over-coverage baseline
    from pre2.recovered.object_update import set_active_zone_margin   # widescreen active-zone cull widener (enemies)
    from pre2.recovered.object_particles import set_item_zone_margins  # widescreen item-zone cull widener (pickups)

    def _smooth_extra(state) -> int:
        """Extraction over-coverage (px/side) the SMOOTH CAMERA extracts then crops: enough for BOTH the
        band-drag deviation (``CROP``) AND pinning the view at a world edge (the display margin ``m_disp``), so
        no beyond-the-world void shows at the level ends. 0 when the smooth camera is off. Equals CROP for every
        window up to ~21:9; only super-ultrawide (m_disp > CROP) widens it. 0 on ROOM levels — their camera is
        band-locked (the tower jumps between sections), so band-drag has nothing meaningful to follow there."""
        return max(_CAM_CROP, _margin_for(state)) if settings["smooth_camera"] and not _room_locked(state) else 0

    def _widehud_frame(rgb, margin: int):
        """A 4:3-gameplay-with-WIDE-HUD frame: centre the faithful 320 gameplay in a ``320+2*margin`` frame with
        BLACK side borders (rows 0..175), and edge-extend the HUD strip (rows 176..199) to the full width. Used
        for the widescreen-excluded boss levels so they keep a widescreen HUD without a widescreen VIEWPORT."""
        if margin <= 0:
            return rgb
        w = 320 + 2 * margin
        out = np.zeros((rgb.shape[0], w, 3), np.uint8)
        out[:VIEWPORT_H, margin:margin + 320] = rgb[:VIEWPORT_H]                       # gameplay centred, black sides
        out[VIEWPORT_H:] = np.pad(rgb[VIEWPORT_H:], ((0, 0), (margin, margin), (0, 0)), mode="edge")  # HUD widened
        return out
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

    def save_screenshot():
        """F12: save the current frame (the clean game image, no letterbox) as a timestamped PNG under
        <game-root>/screenshots/."""
        rgb = ref.get("last")
        if rgb is None:
            return
        import time as _t
        out = Path(gr) / "screenshots"
        try:
            out.mkdir(exist_ok=True)
            arr = np.asarray(rgb, np.uint8)
            surf = pygame.Surface((arr.shape[1], arr.shape[0]))
            pygame.surfarray.blit_array(surf, arr.swapaxes(0, 1))
            path = out / f"pre2_{_t.strftime('%Y%m%d_%H%M%S')}.png"
            pygame.image.save(surf, str(path))
            print(f"screenshot saved: {path}")
        except Exception as e:                                            # noqa: BLE001 — read-only dir / no PIL
            print(f"(screenshot failed: {type(e).__name__}: {e})")

    def pump():
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                ref["running"] = False
            elif ev.type == pygame.VIDEORESIZE and not settings["fullscreen"]:
                disp.resize(ev.w, ev.h)
            elif (ev.type == pygame.KEYDOWN and ev.key == pygame.K_RETURN
                  and ev.mod & pygame.KMOD_ALT):                   # Alt+Enter = fullscreen toggle (the classic)
                set_fullscreen(not settings["fullscreen"])
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F10:
                ref["menu_request"] = True
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F12:
                save_screenshot()
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F11 and args.debug:
                ref["snap_request"] = True                         # DEBUG: dump a --snapshot-loadable native savestate
            elif (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_UP, pygame.K_KP8)
                  and settings["responsive_controls"]):
                ref["jump_edge"] = True                            # RESPONSIVE CONTROLS: catch every jump tap edge,
                #   even one that goes down+up between two ~23 Hz ticks (invisible to the once-per-tick poll below)
            elif ev.type == pygame.JOYDEVICEADDED:                 # GAMEPAD hot-plug
                _open_pad(ev.device_index)
            elif ev.type == pygame.JOYDEVICEREMOVED:
                pads.pop(ev.instance_id, None)
            elif (ev.type == pygame.JOYBUTTONDOWN and ev.button in _PAD_JUMP_BTNS
                  and settings["responsive_controls"]):
                ref["jump_edge"] = True                            # buffer a controller jump tap the same way
            elif (ev.type == pygame.JOYHATMOTION and ev.value[1] > 0
                  and settings["responsive_controls"]):
                ref["jump_edge"] = True                            # D-pad up (SDL +Y) = jump -> buffer its edge too
            elif ev.type == pygame.KEYDOWN:                        # latch the hex make code typed THIS frame
                sc = _SDL_HEX.get(getattr(ev, "scancode", -1)) or _KEYSYM_HEX.get(ev.key)   # physical, keysym fallback
                if sc:
                    ref["last_scan"] = sc
        if ref["menu_request"]:
            ref["menu_request"] = False
            menu_modal()                                          # EVERY loop pumps -> the F10 menu opens anywhere
            #   (late-bound: defined below in main(); no loop runs before it exists)
        if ref["snap_request"]:                                   # DEBUG (F11): dump the live state as a snapshot
            ref["snap_request"] = False
            if ref["state"] is not None:
                dump_gap_snapshot(ref["state"], "F11 debug snapshot", prefix="native_snap")  # late-bound like menu_modal
            else:
                print("  (F11: no live game state to snapshot yet)")

    def blit_frame(rgb):
        """Draw + letterbox one game frame via the GPU (or software fallback); no present yet. Returns its
        on-screen rect."""
        disp.integer_scale = settings["integer_scale"]
        disp.par = _pixel_par()                              # 4:3 (1.2) vs square (1.0) display pixel aspect
        return disp.draw_game(rgb)

    _hud = {"font": None, "t0": 0.0, "ticks0": 0, "text": ""}
    _FRAME_CAPS = [0, 60, 120, 144, 240, -1]              # Display(auto) -> fixed caps -> Uncapped

    def present_hz() -> float:
        """The interpolated-presentation rate from the Frame-cap setting (0=detected display Hz)."""
        cap = settings["frame_cap"]
        return ref["display_hz"] if cap == 0 else (0.0 if cap < 0 else float(cap))

    def _ui_scale() -> float:
        """The F10-overlay UI scale, so it stays a readable PHYSICAL size on hi-DPI / 4K / fullscreen windows
        (the window is now DPI-aware = real pixels). "auto" = proportional to the window height (clamped);
        else the user's fixed percentage (100/150/200/300)."""
        os_scale = settings.get("overlay_scale", "auto")
        if os_scale != "auto":
            try:
                return max(0.5, float(os_scale) / 100.0)
            except (TypeError, ValueError):
                pass
        h = disp.get_size()[1]
        return max(1.0, min(3.5, h / 600.0))

    def pace(fps: float) -> None:
        """Frame pacing: pygame's Clock.tick sleeps in ~ms grains (SDL_Delay), which lands ~60fps when asked
        for 240 — use the busy-wait variant for high rates (precise), plain tick otherwise, none for uncapped."""
        if fps <= 0:
            clock.tick()                                   # uncapped (still updates get_fps)
        elif fps > 90:
            clock.tick_busy_loop(fps)
        else:
            clock.tick(fps)

    def present(rgb, fps, caption=None):
        blit_frame(rgb)
        if settings["fps_overlay"]:
            import time as _time
            us = _ui_scale()                                     # scale the readout with the UI (hi-DPI / 4K)
            if _hud["font"] is None or _hud.get("font_scale") != us:
                from overlay_menu import _load_font              # the same clean UI face as the F10 menu
                _hud["font"] = _load_font(pygame, max(9, int(round(13 * us))), False)
                _hud["font_scale"] = us
                _hud["surf"] = None
            font = _hud["font"]
            now = _time.perf_counter()
            if now - _hud["t0"] >= 0.5:                          # MEASURED rates over a rolling half-second:
                tps = (ref["tick_count"] - _hud["ticks0"]) / (now - _hud["t0"])   # real game ticks/sec (the
                _hud["t0"], _hud["ticks0"] = now, ref["tick_count"]               # proof enhancements never
                _hud["text"] = (f"{clock.get_fps():3.0f} fps  {tps:5.1f} tps"     # touch the tick cadence)
                                if tps > 0 else f"{clock.get_fps():3.0f} fps")
                _hud["surf"] = None                              # re-render the cached text surface
            surf = _hud.get("surf")
            if surf is None:                                     # cache: render text (and its backing box)
                text = font.render(_hud["text"], True, (190, 210, 190))          # once per 0.5s, not per frame
                px, py = int(round(5 * us)), int(round(3 * us))
                surf = pygame.Surface((text.get_width() + 2 * px, text.get_height() + 2 * py))
                surf.fill((10, 12, 14))                          # opaque black box: readable over the game AND
                surf.blit(text, (px, py))                        # self-erasing over the letterbox (no ghosting,
                _hud["surf"] = surf                              #  which the fill-once letterbox left behind)
            disp.draw_overlay(surf, (int(round(8 * us)), int(round(14 * us))))
        disp.flip()
        pace(fps)
        if caption:
            pygame.display.set_caption(caption)
        ref["last"] = rgb

    def dump_gap_snapshot(state, msg: str, prefix: str = "native_gap") -> str | None:
        """Write the CURRENT native state as a repro snapshot the workbench loads directly:
        ``<dir>/memory_1mb.bin`` (the full 1.25 MB image — ``--snapshot <dir>`` re-seeds from it, and every
        probe/oracle does ``NativeGameState(bytearray(read_bytes()))``) + ``state.json`` (the gap message +
        the key game state for triage). Frozen exe -> next to the game data (discoverable); repo -> artifacts/.
        ``prefix`` names the dir: ``native_gap`` for a real gap, ``native_snap`` for a deliberate F11 dump."""
        import datetime
        import json
        try:
            base = Path(gr) if getattr(sys, "frozen", False) else ROOT / "artifacts"
            out = base / f"{prefix}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
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
        pad = pad_scancodes() if pads else set()                   # GAMEPAD: same scancodes the keyboard writes
        held |= (pad - {0x48})                                     # dirs / attack / start straight in; jump via buffer
        jump = bool(k[pygame.K_UP] or k[pygame.K_KP8] or (0x48 in pad))
        if settings["responsive_controls"]:
            # Jump = the UP key (scancode 0x48 -> flag [0x27EA] -> FSM anim 2). The FSM reads the *held* state
            # once per tick, so a fast tap or a slightly-early press falls through the cracks. Re-arm the buffer on
            # each fresh key-down edge (captured in pump()) and keep UP virtually held for a few ticks. Pure
            # input-layer: writes only the same key-table flag a real keyboard would, so gameplay stays untouched
            # when this toggle is off. (Buffering only UP, not fire/direction, keeps movement 1:1.)
            if ref["jump_edge"]:
                ref["jump_buf"] = _JUMP_BUFFER_TICKS
                ref["jump_edge"] = False
            if jump or ref["jump_buf"] > 0:
                held.add(0x48)
            if ref["jump_buf"] > 0:
                ref["jump_buf"] -= 1
        elif jump:
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
        # The Ctrl+Alt+<letter> Easter-egg combos: Left-Ctrl (0x1D) + Left-Alt (0x38) + W (0x11, the 247B
        # dev-credits combo) / E (0x12, the 25C7 game-over creators-photo combo). Without these the combos are
        # only reachable from a recorded demo's raw scancodes, never from a live keyboard.
        for key, sc in ((pygame.K_LCTRL, 0x1D), (pygame.K_LALT, 0x38), (pygame.K_w, 0x11), (pygame.K_e, 0x12),
                        (pygame.K_F1, 0x3B), (pygame.K_F2, 0x3C)):   # F1 = lose a life, F2 = abort->game over
            if k[key]:
                held.add(sc)
        for sc in set(DemoInput.STD) | held | {0x1D, 0x38, 0x11, 0x12, 0x3B, 0x3C}:
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
    audio_post = None                                               # the sink's command inlet (menu toggles post here)
    try:
        from sdl_view import SdlEnhancedAudio
        from pre2.native.audio import NativeAudio
        audio_post = SdlEnhancedAudio(pygame, gr, {}).post
        native_audio = NativeAudio(audio_post, gr)
    except Exception as e:                                          # noqa: BLE001 — no audio device -> run silent
        print(f"  (audio disabled: {type(e).__name__}: {str(e)[:60]})")

    def _audio_apply_settings():
        """Push the music/sfx settings into the audio sink (SetMusicEnabled / SetSfxEnabled events)."""
        if native_audio is not None:
            native_audio.stereo = bool(settings["stereo_sfx"])         # ENHANCED: pan SFX by on-screen X
        if audio_post is None:
            return
        from pre2.audio.events import SetMusicEnabled, SetSfxEnabled
        audio_post(SetMusicEnabled(enabled=bool(settings["music"])))
        audio_post(SetSfxEnabled(enabled=bool(settings["sfx"])))

    _audio_apply_settings()                                         # honour persisted settings from launch

    # --- smooth transitions (Experimental): render level-start/level-end effects full-width + smoothly over the
    #     composed WIDESCREEN frame, instead of streaming the faithful pillarboxed 320px transition frames. Pure
    #     presentation (reads state, writes nothing); the smooth projections live in pre2.enhanced.transitions. ---
    _tx = {"tex": None, "bg": None}

    def _smooth_active() -> bool:
        # Smooth transitions are INDEPENDENT of widescreen: they re-author the curtain/iris/fade present-time
        # (frame-rate-independent) at whatever width the compose runs (320 when widescreen is off, wide when on).
        return bool(settings["smooth_transitions"])

    def compose_wide_now(state, dos, present_cam=None):
        """Compose the CURRENT gameplay state as a widescreen RGB frame (static, alpha=1). Returns
        (rgb, margin_left, efs, sprite_dx) or None if this is not a gameplay frame (no object camera).
        ``present_cam`` (the smooth camera's window-space position) freezes the frame at the EASED camera —
        matching the last displayed gameplay frame so a transition doesn't jump the view; it extracts the CROP
        margin, composes at that camera, and crops it back to the display width. ``sprite_dx`` is the offset to
        add to an efs sprite's screen_x to place it in the returned (possibly cropped) frame (0 unless eased)."""
        from pre2.bridge.foreground_tiles import read_foreground_state
        from pre2.bridge.gameplay_effects import capture_gameplay_effects
        from pre2.enhanced.compositor import compose
        from pre2.enhanced.extract import extract_enhanced_frame
        from pre2.enhanced.native_background import TileTextureCache, _HudCache
        from pre2.enhanced.smooth_camera import CROP as _CAM_CROP, Y_V_PAD as _Y_V_PAD
        from pre2.enhanced.sprite_cache import SpriteTextureCache
        if _tx["tex"] is None:
            _tx["tex"] = SpriteTextureCache()
            _tx["bg"] = (TileTextureCache(), _HudCache())
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        fg = read_foreground_state(state)
        fg.page = disp & 0xFFFF
        fx = capture_gameplay_effects(state, particle_frame=getattr(state, "particle_capture_last", None),
                                      foreground_frame=fg)
        m = _margin_for(state)
        pad = _smooth_extra(state) if present_cam is not None else 0    # match the gameplay smooth-cam coverage
        _room = _room_locked(state)
        _wc = settings["true_widescreen"] and not _room
        efs = extract_enhanced_frame(state, dos, game_root=gr, with_faithful=False, tex_cache=_tx["tex"],
                                     bg_cache=_tx["bg"], effects=fx, margin=m + pad,
                                     wide_cull=_wc, slide_margins=_wc and pad == 0,
                                     hud_align=settings["hud_align"], bg_mode=settings["widescreen_bg"],
                                     bd_pad=pad, room_mode=_room, v_pad=_Y_V_PAD if pad else 0)
        if efs is None:
            return None
        if pad:                                                  # freeze at the eased camera; compose crops the pad
            bg_dx = round(efs.camera[0] - present_cam[0])        # world shift compose applied (= DOS - scam)
            rgb = compose(efs, efs, 1.0, present_cam=present_cam, crop=pad,   # crop-aware -> already display width
                          shake=efs.row_factor)                  # camera-shake jolt ([0x6BF8]; see compose)
            # sprite_dx: efs sprite screen_x (in the m+pad margin) -> the cropped frame; center_dx: a DOS-screen
            # point (0..320) -> the cropped frame. They differ by the cropped-away pad.
            return rgb, m, efs, bg_dx - pad, bg_dx
        return compose(efs, None, 1.0), m, efs, 0, 0

    def _animate(state, duration, render, caption="PRE2 VM-less — transition"):
        """Present ``render(progress 0..1)`` at the DISPLAY rate for ``duration`` wall-clock seconds, so the
        effect is smooth AND frame-rate-INDEPENDENT (progress comes from the clock, not a fixed source step
        count). Ends exactly at progress 1.0. Pumps + polls audio each displayed frame."""
        from time import perf_counter
        t0 = perf_counter()
        while ref["running"]:
            p = (perf_counter() - t0) / duration if duration > 0 else 1.0
            present(render(min(1.0, p)), present_hz(), caption)
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if p >= 1.0 or not ref["running"]:
                break

    def _curtain_frame(rgb, p):
        """Center-out curtain of ``rgb`` at progress ``p`` — VIEWPORT only; the HUD strip is static chrome."""
        import numpy as np
        w = rgb.shape[1]
        cx = w // 2
        half = int(round(min(1.0, max(0.0, p)) * (w / 2.0)))
        fr = np.zeros_like(rgb)
        lo, hi = max(0, cx - half), min(w, cx + half)
        fr[:VIEWPORT_H, lo:hi] = rgb[:VIEWPORT_H, lo:hi]
        fr[VIEWPORT_H:] = rgb[VIEWPORT_H:]
        return fr

    def _present_smooth_reveal(state, dos):
        """The level-start CURTAIN reveal, smooth + full-width + FRAME-RATE-INDEPENDENT: compose the loaded
        level once, then wipe it in center-out over ``_REVEAL_S`` wall-clock seconds at the display rate.
        Returns True if it ran (smooth mode + a gameplay frame)."""
        wide = compose_wide_now(state, dos)
        if wide is None:
            return False
        rgb = wide[0]
        _animate(state, _REVEAL_S, lambda p: _curtain_frame(rgb, p), "PRE2 VM-less — level start")
        return True

    def reveal_level(state, dos):
        """Curtain the freshly-loaded level in (the VM's 3054 center-out level-start reveal) instead of it
        appearing instantly. Driven once at every level start (cold boot + between-levels)."""
        if _smooth_active() and _present_smooth_reveal(state, dos):
            return
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        for planes, page in native_level_reveal(state, dos, disp, game_root=gr):
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                    "PRE2 VM-less — level start")
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return

    # ---- ENHANCED front-end map screens: the CARTE scroll-in + the MODE-SELECT/PASSWORD map render widescreen
    #      and display-rate SMOOTH from the recovered ingredients the scenes carry (FrontEndScene.enh); verified
    #      pixel-exact vs the faithful raster at W=320 (menu 0-diff; carte <=0.03% = the mid-blit frontier col).
    fe = {"carte": None, "menu": None, "prev": None, "due": None,
          "fade_shape": None, "fade_disp": None, "fade_due": None, "fade_last": None}
    _FADE_TAU = 0.07   # seconds — see present_front_scene's DAC-fade branch

    def _fade_shape(scene):
        """An identity key for a scene's IMAGE content (ignoring its palette) — planes/linear are reused
        verbatim (same object) across every step of a front-end DAC fade (front_end.py's fade generators
        decode/freeze the picture ONCE, then only swap `palette` per yield), so `id()` is a cheap, reliable
        same-picture test."""
        return (scene.mode, id(scene.planes), id(scene.linear), scene.page, scene.pel, scene.wrap,
                scene.active_width)

    def present_front_scene(scene, fps, caption=None):
        """Present one front-end scene: the scrolling map screens (scene.enh) render ENHANCED — widescreen
        (real 640px map content on the carte; the seamlessly-wrapping bg ring on the menu) + sub-frame smooth
        pan when Interpolation is on. Everything else presents the faithful raster, EXCEPT a Smooth
        transitions DAC fade (OLDIES/title/menu-entry/game-over/THE END).

        The recovered DAC fade (front_end_fade.py) doesn't move every retrace: a component only advances on
        the ~1-in-8 retrace where its turn comes up in the pass (256-entry fades retrace every 32 entries, 8
        of them per pass), so the RAW keyframe sequence is a staircase — ~7 held frames then an 8-9-unit (of
        255) jump, repeating. Linearly interpolating each keyframe into the NEXT one (an earlier attempt)
        just spreads that same jump over 14ms and then holds again for ~100ms — a hold that long still reads
        as a visible step no matter how smoothly the jump itself is drawn. Fix: don't interpolate keyframe to
        keyframe at all — keep a PERSISTENT displayed palette that continuously chases the latest keyframe
        (an exponential filter, time-constant `_FADE_TAU`), sampled every presented sub-frame. That low-pass
        filters the staircase into a genuinely continuous ramp — no dependence on how the discrete algorithm
        happens to be paced — and it still converges to the exact final colour once the target stops moving
        (a hold/steady phase yields the SAME palette repeatedly, so the filter fully catches up long before
        the scene changes; snapped exactly once within half a unit to avoid a perpetual asymptotic tail).
        Smooth transitions off (or a real picture change — e.g. the title's logo appearing) presents the
        exact original discrete sequence, byte-for-byte."""
        enh_on = scene.enh is not None and (settings["widescreen"] or settings["interpolation"])
        if not enh_on:
            fe["prev"] = fe["due"] = None
            shape = _fade_shape(scene)
            if not settings["smooth_transitions"] or fe["fade_shape"] != shape:
                fe["fade_shape"] = shape
                fe["fade_disp"] = None
                fe["fade_due"] = None
                present(front_end_scene_to_rgb(scene), fps, caption)
                return
            from dataclasses import replace
            from time import perf_counter
            target = np.asarray(scene.palette, dtype=np.float32)
            disp = fe["fade_disp"] if fe["fade_disp"] is not None else target.copy()
            now = perf_counter()
            if fe["fade_due"] is None or fe["fade_due"] < now - 0.25:
                fe["fade_due"] = now
                fe["fade_last"] = now
            step = 1.0 / fps
            while True:
                now = perf_counter()
                dt = max(0.0, now - fe["fade_last"])
                fe["fade_last"] = now
                k = 1.0 - np.exp(-dt / _FADE_TAU)
                disp = disp + (target - disp) * k
                if np.abs(disp - target).max() < 0.5:               # fully caught up -> snap exact (no
                    disp = target.copy()                             # perpetual asymptotic tail)
                pal = [tuple(row) for row in np.round(disp).astype(np.uint8).tolist()]
                mid = replace(scene, palette=pal)
                present(front_end_scene_to_rgb(mid), present_hz(), caption if now >= fe["fade_due"] else None)
                if now >= fe["fade_due"]:
                    break
                pump()
                if not ref["running"]:
                    break
            fe["fade_disp"] = disp
            fe["fade_due"] += step
            return
        from pre2.enhanced.front_scenes import CarteEnh, MenuEnh
        from time import perf_counter
        W = 320 + 2 * wide_margin()
        kind = scene.enh[0]
        if kind == "carte":
            if fe["carte"] is None:
                fe["carte"] = CarteEnh()
            cur = float(scene.enh[2])
            render = lambda s: fe["carte"].frame(scene.enh[1], s, scene.palette, W)     # noqa: E731
        else:
            if fe["menu"] is None:
                fe["menu"] = MenuEnh()
            cur = float(MenuEnh.pan_px(scene))
            render = lambda s: fe["menu"].frame(scene, s, scene.palette, W, front_end_scene_to_rgb)  # noqa: E731
        prev = fe["prev"] if (fe["prev"] is not None and fe["prev"][0] == kind) else None
        fe["prev"] = (kind, cur)
        if not settings["interpolation"]:                      # widescreen only: one wide frame per scene
            fe["due"] = None
            present(render(cur), fps, caption)
            return
        # Display-rate smoothing: sub-frames lerp the HORIZONTAL pan between the prev and cur scene positions
        # (fixed-timestep accumulator like present_interpolated). The menu's linear-ring pan folds its vertical
        # sine-bounce in as whole ±320px rows — interpolate only the horizontal residue (rows step faithfully;
        # lerping them would read as a horizontal glitch, not a vertical bounce).
        now = perf_counter()
        if fe["due"] is None or fe["due"] < now - 0.25:
            fe["due"] = now
        dx = 0.0
        if prev is not None:
            d = cur - prev[1]
            if kind == "menu":
                d = ((d + 32768.0) % 65536.0) - 32768.0        # shortest path around the 65536px ring
                d = ((d + 160.0) % 320.0) - 160.0              # the horizontal residue of the linear pan
            if abs(d) <= 16.0:
                dx = d
        step = 1.0 / fps
        while True:
            now = perf_counter()
            alpha = min(1.0, max(0.0, 1.0 - (fe["due"] - now) / step))
            present(render(cur - (1.0 - alpha) * dx), present_hz(), caption if alpha >= 1.0 else None)
            if perf_counter() >= fe["due"]:
                break
            pump()
            if not ref["running"]:
                break
        fe["due"] += step

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

        print("  level complete -> IRIS close -> exit-anim (walk-in + food-throw score count-up + walk-off) -> carte")
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        # freeze the iris at the eased camera when the smooth camera is on, so the view doesn't jump on close
        _pcam = ref.get("last_pcam") if settings["smooth_camera"] else None
        smooth = compose_wide_now(state, dos, present_cam=_pcam) if _smooth_active() else None
        if smooth is not None:
            # SMOOTH iris: drain native_iris_close for its STATE work (reading the recovered iris centre off the
            # first frame), then present-time animate a true circle 0xE6->0 over the frozen wide frame at the
            # display rate (frame-rate-independent), the player held on top.
            from pre2.bridge.render_state import read_renderer_state
            from pre2.enhanced.compositor import _blit
            from pre2.enhanced.transitions import apply_iris
            rgb, m, efs, sdx, cdx = smooth                        # sdx/cdx: eased-camera offsets (sprite / centre)
            players = [i for i in efs.sprites if i.handle == ("player",)]
            # Drain native_iris_close for its STATE work (it advances the recovered iris close); the DOS iris
            # centre it exposes is CLAMPED to the 320 view, so it misses the player when the smooth camera has
            # them out in a widescreen margin. Centre on the PLAYER'S OWN displayed position instead (the exact
            # coords the player blit below uses) so the iris always closes on the player, wherever they are.
            dos_center = None
            for planes, page in native_iris_close(state, dos, disp, game_root=gr):
                if dos_center is None:
                    iris = read_renderer_state(state, dos).iris
                    if iris is not None:                           # fallback centre: COL = center_y (+margin +
                        dos_center = (iris.center_y + m + cdx, iris.center_x)   # eased shift), ROW = center_x
                pump()
                if native_audio is not None:
                    native_audio.poll(state)
                if not ref["running"]:
                    return
            if players:                                            # target the player exactly (margin-safe)
                p = players[0]
                ph, pw = p.rgba.shape[:2]
                center = (p.screen_x + p.tex_off_x + sdx + pw // 2, p.screen_y + p.tex_off_y + ph // 2)
            elif dos_center is not None:
                center = dos_center
            else:
                center = (rgb.shape[1] // 2, _VFADE_MID)

            def _iris_fr(p):
                fr = rgb.copy()
                r = 0xE6 * (1.0 - p)
                if r > 0:
                    apply_iris(fr, r, center[0], center[1])
                else:
                    fr[:] = 0
                for inst in players:
                    _blit(fr, inst.rgba, inst.screen_x + inst.tex_off_x + sdx, inst.screen_y + inst.tex_off_y)
                return fr
            _animate(state, _IRIS_S, _iris_fr, "PRE2 VM-less — level complete")
        else:
            for planes, page in native_iris_close(state, dos, disp, game_root=gr):   # 316F circle-close (faithful)
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
            present_front_scene(scene, _FRONT_END_FPS, "PRE2 VM-less — world map")
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
        from pre2.native.front_end import native_creators_screen, native_menu_flow
        from pre2.native.gameover_scene import native_gameover_scene
        from pre2.native.player import ecombo_confirmed
        print("  GAME OVER -> the 9B23 scene -> menu -> carte -> restart")
        pump(); drive_input(state)                                  # refresh the key flags at the game-over moment
        if ecombo_confirmed(state):                                # [asm 506C: call 25C7] Ctrl+Alt+E held at game-over
            print("  Ctrl+Alt+E -> the creators photo (25C7 -> 25F6)")
            for scene in native_creators_screen(state, gr):        # the same mode-12h HOLLY DAY photo as THE END
                present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — creators")
                pump(); drive_input(state)
                if native_audio is not None:
                    native_audio.poll(state)
                if not ref["running"]:
                    return
        native_load_song(state, "BOULA.TRK", gr)                   # [asm 5063: 02CC ax=0x11] the game-over song
        for planes, page in native_gameover_scene(state, dos, gr):  # [asm 9B23] the scene (fire/timeout exits)
            # each scene frame is one 44FB present = 3 retraces (the ASM busy-waits 3 vsyncs), so it displays at
            # _FRONT_END_FPS/3 (~23Hz), NOT the per-retrace front-end rate — else the whole scene runs 3x too fast.
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _FRONT_END_FPS / 3,
                    "PRE2 VM-less — GAME OVER")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # BOULA.TRK
            if not ref["running"]:
                return
        for scene in native_menu_flow(state, dos, gr):             # [main 011C] menu -> map -> carte -> loader
            fps = args.fps if state.data[DS + 0x2879] == 1 else _FRONT_END_FPS
            present_front_scene(scene, fps, "PRE2 VM-less — restart")
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
            present_front_scene(scene, fps, "PRE2 VM-less — restart")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)
        reveal_level(state, dos)

    def show_cheat_credits(state, dos):
        """[asm 247B->2505] The dev-credits cheat combo (Ctrl+Alt+W/Z, no other key). Show the OLDIES-style
        developer-credits screen over black, hold for fire (0BBE), restore the level palette (0BA0), and RESUME
        the same level (the combo is a pure overlay — gameplay state is untouched)."""
        from pre2.bridge.oldies_scene import build_credits_scene
        from pre2.native.front_end import WAIT_PRESS, native_scene_wait
        from pre2.native.render import native_load_dac_palette
        from pre2.native.front_end import FrontEndScene
        from pre2.recovered.scene import MODE_PLANAR
        print("  CHEAT COMBO -> developer credits (247B); press fire to resume")
        native_load_dac_palette(state, dos, 0x287E)                # [asm 0b92] the OLDIES green/yellow palette
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        phase = WAIT_PRESS
        while ref["running"]:
            planes, _ = build_credits_scene(state, page=disp)      # [asm 2505] the dev-name text over black
            scene = FrontEndScene(MODE_PLANAR, palette=tuple(dos.vga_palette),
                                  planes=tuple(bytes(p) for p in planes), page=disp)
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — developer credits")
            pump(); drive_input(state)
            phase, done = native_scene_wait(state, phase)          # [asm 0bbe] fire press -> release
            if done:
                break
        native_load_level_palette(state, dos)                      # [asm 0ba0] restore the level palette; resume

    def _p_edge():
        """A rising edge on the PAUSE control — the P key (scancode 0x19) OR the gamepad Start button. Only called
        from pause_check (mid-gameplay), so Start pauses here while still meaning '1'/begin in the front-end menu.
        pump() must have run this frame first."""
        held = bool(pygame.key.get_pressed()[pygame.K_p])
        if not held and pads:                                      # gamepad Start also toggles pause
            for js in list(pads.values()):
                try:
                    nb = js.get_numbuttons()
                    if any(b < nb and js.get_button(b) for b in _PAD_START_BTNS):
                        held = True
                        break
                except Exception:                                  # noqa: BLE001 — pad unplugged mid-poll
                    continue
        edge = held and not ref["p_prev"]
        ref["p_prev"] = held
        return edge

    def pause_check(state):
        """[asm 6294] The P-key PAUSE: press P to freeze, press P again to resume. 6294 is a pure busy-wait on the
        P-held flag [0x280D] with NO gameplay-state writes, so native excludes it from the frame model and it lives
        here as a presentation freeze — hold the last frame + keep the music alive (interrupts stay live during the
        original's spin, so music kept playing) until the next P edge."""
        if not _p_edge():
            return
        pygame.display.set_caption("PRE2 VM-less gameplay — PAUSED (P or Start resumes)")
        while ref["running"]:
            pump()
            if ref["last"] is not None:
                present(ref["last"], _FRONT_END_FPS)
            else:
                clock.tick(_FRONT_END_FPS)
            if native_audio is not None:
                native_audio.poll(state)                           # music continues while frozen (as in the original)
            if _p_edge():
                break

    # ---- the F10 overlay menu (modal; visual style from pre2_editor) --------------------------------------
    from overlay_menu import OverlayMenu

    def _menu_tabs():
        """The tab/item data (re-evaluated each frame so values render live). Closures edit `settings` +
        `ref` — HOST/presentation state only; the Develop tab (cheats) exists only under --debug."""
        def onoff(k):
            return "On" if settings[k] else "Off"

        def toggle(k, apply=None):
            def act():
                settings[k] = not settings[k]
                if apply:
                    apply()
                save_settings()
            return act

        cap = settings["frame_cap"]
        cap_label = (f"Display ({ref['display_hz']:.0f} Hz)" if cap == 0
                     else "Uncapped" if cap < 0 else f"{cap} Hz")

        def adj_cap(d):
            caps = _FRAME_CAPS
            i = caps.index(cap) if cap in caps else 0
            settings["frame_cap"] = caps[(i + d) % len(caps)]
            save_settings()

        _HUD_ALIGNS = ["left", "center", "right"]

        def adj_hud(d):
            i = _HUD_ALIGNS.index(settings["hud_align"]) if settings["hud_align"] in _HUD_ALIGNS else 1
            settings["hud_align"] = _HUD_ALIGNS[(i + d) % len(_HUD_ALIGNS)]
            save_settings()

        _MENU_SCALES = ["auto", 100, 150, 200, 300]

        def adj_menu_scale(d):
            cur = settings.get("overlay_scale", "auto")
            i = _MENU_SCALES.index(cur) if cur in _MENU_SCALES else 0
            settings["overlay_scale"] = _MENU_SCALES[(i + d) % len(_MENU_SCALES)]
            save_settings()

        _sc = settings.get("overlay_scale", "auto")
        menu_scale_label = "Auto" if _sc == "auto" else f"{_sc}%"

        _BG_MODES = ["stretch", "mirror", "black"]     # widescreen backdrop margin fill

        def adj_bg(d):
            i = _BG_MODES.index(settings["widescreen_bg"]) if settings["widescreen_bg"] in _BG_MODES else 0
            settings["widescreen_bg"] = _BG_MODES[(i + d) % len(_BG_MODES)]
            save_settings()

        from pre2.enhanced.smooth_camera import Y_SMOOTH_LABELS as _CS_LABELS

        def _cs_level():
            return min(max(int(settings.get("camera_smoothing", 0)), 0), len(_CS_LABELS) - 1)

        def adj_camera_smoothing(d):
            settings["camera_smoothing"] = (_cs_level() + d) % len(_CS_LABELS)
            save_settings()

        _ASPECTS = ["square", "4:3"]                   # displayed pixel aspect

        def adj_aspect(d):
            i = _ASPECTS.index(settings.get("pixel_aspect", "square")) if settings.get("pixel_aspect") in _ASPECTS else 0
            settings["pixel_aspect"] = _ASPECTS[(i + d) % len(_ASPECTS)]
            save_settings()

        _ASPECT_LABEL = {"square": "Square (1:1)", "4:3": "4:3 (CRT)"}

        _WS_ASPECTS = ["auto", "16:9", "16:10", "4:3", "21:9", "32:9"]   # widescreen target aspect

        def adj_ws_aspect(d):
            cur = settings.get("widescreen_aspect", "auto")
            i = _WS_ASPECTS.index(cur) if cur in _WS_ASPECTS else 0
            settings["widescreen_aspect"] = _WS_ASPECTS[(i + d) % len(_WS_ASPECTS)]
            save_settings()

        _ws_a = settings.get("widescreen_aspect", "auto")
        _ws_aspect_label = "Auto" if _ws_a == "auto" else _ws_a

        view_tab = [
            {"label": "Interpolation", "value": onoff("interpolation"), "activate": toggle("interpolation")},
            {"label": "Smooth transitions", "value": onoff("smooth_transitions"),
             "activate": toggle("smooth_transitions")},
            {"label": "Frame cap", "value": cap_label, "adjust": adj_cap},
            {"label": "Fullscreen", "value": onoff("fullscreen"),
             "activate": lambda: set_fullscreen(not settings["fullscreen"])},
            {"label": "Integer scaling", "value": onoff("integer_scale"), "activate": toggle("integer_scale")},
        ]
        widescreen_tab = [
            {"label": "Widescreen", "value": onoff("widescreen"), "activate": toggle("widescreen")},
            {"label": "Aspect", "value": _ws_aspect_label, "adjust": adj_ws_aspect},
            {"label": "Pixel aspect", "value": _ASPECT_LABEL.get(settings.get("pixel_aspect", "square"), "Square (1:1)"),
             "adjust": adj_aspect},
            {"label": "Backdrop", "value": settings["widescreen_bg"].capitalize(), "adjust": adj_bg},
            {"label": "HUD position", "value": settings["hud_align"].capitalize(), "adjust": adj_hud},
        ]
        overlay_tab = [
            {"label": "FPS overlay", "value": onoff("fps_overlay"), "activate": toggle("fps_overlay")},
            {"label": "Menu scale", "value": menu_scale_label, "adjust": adj_menu_scale},
        ]
        audio_tab = [
            {"label": "Music", "value": onoff("music"), "activate": toggle("music", _audio_apply_settings)},
            {"label": "Sound effects", "value": onoff("sfx"), "activate": toggle("sfx", _audio_apply_settings)},
            {"label": "Stereo SFX", "value": onoff("stereo_sfx"),
             "activate": toggle("stereo_sfx", _audio_apply_settings)},
        ]
        experimental_tab = [
            {"label": "True widescreen", "value": onoff("true_widescreen"), "activate": toggle("true_widescreen")},
            {"label": "Active zone", "value": onoff("widescreen_active_zone"),
             "activate": toggle("widescreen_active_zone")},
            {"label": "Smooth camera", "value": onoff("smooth_camera"), "activate": toggle("smooth_camera")},
            {"label": "Camera smoothing", "value": _CS_LABELS[_cs_level()], "adjust": adj_camera_smoothing},
            {"label": "Responsive controls", "value": onoff("responsive_controls"),
             "activate": toggle("responsive_controls")},
            {"label": "", "info": True},
            {"label": "Experimental enhancements aren't faithful to the original —", "info": True},
            {"label": "Active zone changes gameplay; Responsive controls buffer the jump key.", "info": True},
        ]
        tabs = [("View", view_tab), ("Widescreen", widescreen_tab), ("Overlay", overlay_tab),
                ("Audio", audio_tab), ("Experimental", experimental_tab)]
        if args.debug:
            lvl = ref.get("menu_level", 0)

            def adj_level(d):
                ref["menu_level"] = (lvl + d) % 0x10       # playable ids 0x00..0x0F (LEVEL1..9, A..G); id 0x10
                #   is NOT a bootable level — the per-level DGROUP tables end at 0x0F (LEVELH/I.SQZ are the
                #   ending-credits scenery the finale loads by its own path, not via the level selector)

            def go_level():
                ref["switch_level"] = ref.get("menu_level", 0)
                menu.open = False

            lvl_name = str(lvl + 1) if lvl < 9 else chr(ord("A") + lvl - 9)   # 0..8 -> 1..9, 9..0xF -> A..G
            tabs.append(("Develop", [
                {"label": "God mode", "value": onoff("god"), "activate": toggle("god")},
                {"label": "Level", "value": f"LEVEL{lvl_name}", "adjust": adj_level, "activate": go_level},
                {"label": "Restart level", "value": "reload current", "activate": lambda: (
                    ref.__setitem__("switch_level", "restart"), setattr(menu, "open", False))},
            ]))
        return tabs

    menu = OverlayMenu(pygame, _menu_tabs)

    def menu_modal():
        """The modal F10 overlay, openable from ANY loop (every scene pumps): the game/scene is frozen while
        open and every key event is routed to the menu — nothing it consumes can reach the game's input
        cells, so demo determinism and the oracle chain are structurally untouched. Music keeps playing
        (the SDL sink streams the current song autonomously; no polling needed while frozen)."""
        menu.open = True
        while ref["running"] and menu.open:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    ref["running"] = False
                elif ev.type == pygame.VIDEORESIZE and not settings["fullscreen"]:
                    disp.resize(ev.w, ev.h)
                elif (ev.type == pygame.KEYDOWN and ev.key == pygame.K_RETURN
                      and ev.mod & pygame.KMOD_ALT):
                    set_fullscreen(not settings["fullscreen"])
                elif ev.type == pygame.KEYDOWN:
                    menu.handle_keydown(ev)
                elif ev.type in (pygame.MOUSEMOTION, pygame.MOUSEWHEEL, pygame.MOUSEBUTTONDOWN):
                    menu.handle_mouse(ev)
            if ref["last"] is not None:
                blit_frame(ref["last"])                            # the frozen game frame (GPU) UNDER the menu
            else:
                disp.draw_game(np.zeros((200, 320, 3), np.uint8))  # menu before the first frame -> over black
            sz = disp.get_size()                                  # draw the menu onto a persistent transparent
            canvas = view.get("menu_canvas")                      # window-size surface, then composite it over
            if canvas is None or canvas.get_size() != sz:         # the game frame
                canvas = view["menu_canvas"] = disp.new_overlay_canvas()
            canvas.fill((0, 0, 0, 0))
            menu.draw(canvas, _ui_scale())
            disp.draw_overlay(canvas, (0, 0))
            disp.flip()
            clock.tick(60)

    def gameplay_loop(state, dos):
        """Run the recovered gameplay VM-less: host input -> native_frame_step -> present, until a gap.

        Structure: one loop iteration = one game TICK (native_frame_step; a transition yields several
        presentation frames within its tick). ``present_tick_frame`` is the single seam where every rendered
        gameplay frame reaches the screen — the (future) interpolation replaces exactly this: hold prev+cur
        FrameSnapshots and present lerped frames at ref["display_hz"] instead of one faithful frame per tick.
        The TICK cadence itself never changes with enhancements — only what is shown between ticks."""
        print("Gameplay — SPACE = fire/jump, arrows/numpad = move, P = pause, ESC = quit. (VM-less native gameplay)")
        if args.debug:
            print("  [debug] F11 = dump a native snapshot (--snapshot-loadable) for repro.")
        ref["state"] = state          # register the live state so the F11 debug hotkey (in pump) can snapshot it
        from time import perf_counter
        from pre2.bridge.foreground_tiles import read_foreground_state
        from pre2.bridge.gameplay_effects import capture_gameplay_effects
        from pre2.enhanced.compositor import compose
        from pre2.enhanced.extract import extract_enhanced_frame
        from dataclasses import replace as _dc_replace
        from pre2.enhanced.native_background import TileTextureCache, _HudCache
        from pre2.enhanced.snow import SnowField
        from pre2.enhanced.sprite_cache import SpriteTextureCache
        from pre2.enhanced.smooth_camera import (CROP as _CAM_CROP, Y_V_PAD, smooth_cam_x, smooth_cam_y,
                                                 world_max_px, y_smooth_tau)
        from pre2.native.render import native_render                # for the enhanced-path faithful fallback
        from pre2.gaps import Pre2CheatCredits, Pre2GameComplete, Pre2GameOverTransition, Pre2LevelEndTransition
        n = 0
        tick_dt = 1.0 / TICK_HZ
        # persistent tile/HUD texture cache (bg) so the per-tick extract reuses baked tile cels instead of
        # re-baking them every tick -- the extract was passing bg_cache=None (throwaway each call).
        enh = {"tex": SpriteTextureCache(), "bg": (TileTextureCache(), _HudCache()),
               "snow": SnowField(), "snow_t": 0.0,   # ENHANCED snow (smooth transitions): wall-clock field
               "prev": None, "next_tick": None,   # the prev+cur snapshot pair
               "last_cur": None, "scam": None, "scam_t": 0.0}   # smooth-tx fade base + smooth-camera state

        def _smooth_cam(cur, alpha):
            """The SMOOTH-CAMERA presentation position, or None when off. X AND Y = the same rigid BAND-DRAG
            (pure — fully decoupled from the DOS camera, whose park-then-pan X / park-then-recenter Y motion is
            exactly what this replaces): the camera holds inside a centered screen band and is dragged by the
            (sub-tick interpolated) player past its edges, rate-limited so it GLIDES rather than ever jumping,
            clamped to the level's real camera bounds (the DOS [0x8164] X limit / [0x2CF5] bottom). Coverage
            for the Y deviation: tile over-extraction + vertical sprite re-admission (extract v_pad). OFF
            (returns None — the faithful FIXED camera is presented + the widescreen margins blacked) in a
            `_room_locked` fixed-camera cave / room level: there is nothing to smooth (the camera never scrolls
            there), and the band would otherwise drag with the player past the room's edge.
            Presentation only; a teleport / level load reseeds."""
            if not settings["smooth_camera"] or enh["prev"] is None or _room_locked(state):
                enh["scam"] = None                                  # (fixed camera: no drag; present the room)
                return None
            prev = enh["prev"]
            inv = 1.0 - alpha
            # SPACES: cur.camera[0] is the frame WINDOW's world-left (= DOS camera - cam_margin_left, the
            # widescreen + smooth-camera margins folded in) — the space compose expects present_cam in. The
            # band math must run in TRUE camera space (comparable with the player's world x), so convert via
            # cam_margin_left both ways. (Feeding window-left into the band/clamps was THE v2-v4 bug: the CROP
            # clamp pinned the view margin-left of the DOS camera — "everything shifted right" — and the band
            # never engaged, so the view just followed the DOS pan steps.)
            ml_cur = cur.cam_margin_left
            cam0 = float(cur.camera[0] + ml_cur)                            # TRUE DOS camera x (this tick)
            prev0 = float(prev.camera[0] + prev.cam_margin_left)
            dosx = cam0 - inv * (cam0 - prev0)                              # the interpolated TRUE DOS camera
            # SHAKE-FREE DOS cam_y (== cam_y*16+fine; camera[1] folds in -row_factor, the [0x6BF8] landing jolt).
            # The smooth view is shake-free (so is the tile slice), so follow the shake-free cam, interpolated.
            cury_free = float(cur.camera[1] + cur.row_factor)
            dosy = cury_free - inv * (cury_free - float(prev.camera[1] + prev.row_factor))
            now = perf_counter()
            dt = min(0.05, max(0.0, now - enh["scam_t"]))
            enh["scam_t"] = now
            pc = next((i for i in cur.sprites if i.handle == ("player",)), None)
            s = enh["scam"]                                     # [x, y] presentation-cam state
            if s is not None and (abs(cam0 - s[0]) > 240 or abs(dosy - s[1]) > 240):   # teleport/load -> reseed
                s = enh["scam"] = None
            # DEATH: while a death-bounce is armed ([0x6be4]==1 respawn / [0x6be5]!=0 game-over — NOT the boss-hit
            # counter [0x6be4]==2, where the player is alive + flashing), FREEZE the camera. The dying player arcs
            # up then falls THROUGH the floor; the band would chase the corpse off-screen. Hold the pre-death view.
            dying = state.data[DS + 0x6BE4] == 1 or state.data[DS + 0x6BE5] != 0
            if pc is None or dying:                                        # no player this frame (rare) / dying ->
                return (s[0] - ml_cur, s[1]) if s is not None else None     # hold X+Y (back in window space)
            pp = next((i for i in prev.sprites if i.handle == ("player",)), None)
            pwx, pvx = float(pc.world_x), 0.0
            pwy, pvy = float(pc.world_y), 0.0                              # baseline == the record [0x4F1E]
            if pp is not None and abs(pc.world_x - pp.world_x) <= 32:      # the player's sub-tick position +
                dx_t = float(pc.world_x - pp.world_x)                      # velocity (px/s) for the rate limit
                pwx -= inv * dx_t
                pvx = dx_t * TICK_HZ
            if pp is not None and abs(pc.world_y - pp.world_y) <= 32:      # sub-tick Y (the Y band-drag input)
                dy_t = float(pc.world_y - pp.world_y)
                pwy -= inv * dy_t
                pvy = dy_t * TICK_HZ
            if s is None:
                s = enh["scam"] = [float(dosx), float(dosy), 0.0]         # seed at the current view; glide in
            w8164 = state.data[DS + 0x8164] | (state.data[DS + 0x8165] << 8)
            # m_disp = the displayed margin (clamp the whole widescreen window inside the world -> no edge void);
            # crop = the extracted over-coverage (must cover pinning at the edge). Both live off the window size.
            s[0] = smooth_cam_x(s[0], pwx, pvx, dt, cam0, world_max_px(w8164, pwx),
                                m_disp=float(_margin_for(state)), crop=float(_smooth_extra(state)))
            # Y: the SAME band-drag, vertical, + the level's airborne look-ahead as a smooth velocity bias
            # (see smooth_camera.py) — decoupled from the DOS camera's park-then-recenter bands. The coverage
            # clamp is vs the CURRENT tick's shake-free cam (cury_free) because the compositor slices the tile
            # layer at cury_free - present_cam_y. A forced-auto-scroll level ([0x8166]&4: the camera moves on
            # its own) follows the interpolated DOS cam instead — a band would fight the auto-scroll.
            bottom_px = max(0, state.data[DS + 0x2CF5] - 0xB) * 16.0
            if state.data[DS + 0x2D8A] == 13:              # LEVEL 13 (flat; the earthquake pillars are STAGED
                # underground, under the floor tiles): a level-specific VIRTUAL bottom edge so the camera never
                # scrolls down into that black under-level area. The DOS [0x2CF5] limit (432px) is far below the
                # floor here; the edge is the level's resting DOS cam_y (160) minus 11px = 149 (measured on
                # native_snap_20260707_220659). Clamps the camera's lowest position, hiding the staged pillars.
                bottom_px = min(bottom_px, 149.0)
            forced = float(dosy) if (state.data[DS + 0x8166] & 4) else None
            s[1], s[2] = smooth_cam_y(s[1], s[2], pwy, pvy, dt, cury_free, bottom_px, dos_follow=forced,
                                      tau=y_smooth_tau(settings["camera_smoothing"]))
            return (s[0] - ml_cur, s[1])                                    # back to WINDOW space for compose

        def present_tick_frame(planes, page):
            """Present one faithful gameplay frame, paced at the tick rate (the enhancement seam)."""
            nonlocal n
            rgb = render_planar_rgb_from_planes(planes, page, dos.vga_palette)
            # Widescreen-excluded levels (LEVEL A/F boss) render the GAMEPLAY faithfully (4:3, black borders) but
            # still get a WIDE HUD when Widescreen is on — "some widescreen feeling" without the tile-bleed / boss
            # issues. Pure post-process on the finished 320 frame, so the fight's render + timing are untouched.
            if settings["widescreen"] and state.data[DS + 0x2D8A] in _WS_EXCLUDE_LEVELS:
                rgb = _widehud_frame(np.asarray(rgb, np.uint8), wide_margin())
            n += 1
            present(rgb, args.fps, None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")

        def present_interpolated(planes, page):
            """The INTERPOLATION presentation for a steady (single-frame) tick: extract the tick's snapshot,
            then present compose(cur, prev, alpha) frames at the DISPLAY rate until the next tick is due
            (a fixed-timestep accumulator — the tick cadence is enforced by wall-clock due times, never
            changed by how many frames are shown between ticks). Pure presentation: reads the state, writes
            nothing — the endpoints are parity-proven pixel-equal to the faithful frames (alpha=1 gate)."""
            # The EFFECTS bundle, mirroring native_render's own construction (same sources; the tick's
            # native_render already consumed the one-shots, so use the *_last stashes it leaves): point
            # particles (spider threads/sparkles), foreground tiles (z-order OVER sprites), fireflies, snow.
            nonlocal n
            disp2 = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
            fg = read_foreground_state(state)
            fg.page = disp2 & 0xFFFF
            fx = capture_gameplay_effects(state, particle_frame=getattr(state, "particle_capture_last", None),
                                          foreground_frame=fg)
            # ENHANCED SNOW (part of Smooth transitions): replace the tick-rate 320px faithful plot list with
            # the wall-clock SnowField drawn over the whole (possibly wide) presented frame. The gameplay tick
            # still ran the real scroll_script_snow (its shared-rng advance is byte-exact-critical); only the
            # DRAWING is swapped. Off -> the faithful plots render via the overlay exactly as before.
            _wind = state.data[DS + 0x6BF6] | (state.data[DS + 0x6BF7] << 8)
            _snow_on = settings["smooth_transitions"] and _wind > 0
            if _snow_on and fx.snow is not None:
                fx = _dc_replace(fx, snow=None)

            def _snow_over(frame):
                """Advance + draw the enhanced snow onto a presented frame (no-op when inactive)."""
                if _snow_on:
                    now2 = perf_counter()
                    enh["snow"].draw(frame, _wind, now2 - enh["snow_t"],
                                     rgb=tuple(int(v) for v in (dos.vga_palette or [(255,) * 3] * 16)[15]))
                    enh["snow_t"] = now2
                return frame
            # Re-apply the one-frame OPAQUE flash bits (cleared during the tick) around the snapshot, exactly
            # like native_render — restore right after so carried-forward state stays byte-exact.
            flash = getattr(state, "flash_slots_last", None)
            saved = None
            if flash:
                saved = [(off, state.data[DS + off + 5]) for off in flash]
                for off in flash:
                    state.data[DS + off + 5] |= 0x40
            # SMOOTH CAMERA extracts CROP px of extra tile margin each side (the deviation the presentation
            # camera may shift the view by); _crop() cuts it back off after compose, so the DISPLAYED width
            # stays the widescreen width and the shifted-in edges always show real extracted content.
            m_extra = _smooth_extra(state)

            def _crop(frame):
                # IDEMPOTENT: compose(crop=m_extra) already returns the display width for the smooth-camera
                # (present_cam) subframes -> no-op; only the present_cam=None subframes arrive over-extracted.
                if m_extra and frame.shape[1] > 320 + 2 * _margin_for(state):
                    return np.ascontiguousarray(frame[:, m_extra:frame.shape[1] - m_extra])
                return frame
            try:
                # (true widescreen's world-edge margin SLIDE is incompatible with the symmetric smooth-camera
                # crop, so it's off while the smooth camera drives; margin objects still draw via the margin.)
                _room = _room_locked(state)                     # room level OR a bounded room here: black margins
                _wc = settings["true_widescreen"] and not _room
                cur = extract_enhanced_frame(state, dos, game_root=gr, with_faithful=False,
                                             tex_cache=enh["tex"], bg_cache=enh["bg"], effects=fx,
                                             margin=_margin_for(state) + m_extra,
                                             wide_cull=_wc, slide_margins=_wc and m_extra == 0,
                                             hud_align=settings["hud_align"],
                                             bg_mode=settings["widescreen_bg"], bd_pad=m_extra, room_mode=_room,
                                             v_pad=Y_V_PAD if m_extra else 0)
            finally:
                if saved is not None:
                    for off, v in saved:
                        state.data[DS + off + 5] = v
            if cur is None:                                     # no snapshot -> one faithful frame
                if planes is None:                              # raster was skipped -> render it now for the fallback
                    planes, page = native_render(state, dos, disp2, game_root=gr, force_gameplay=True)
                present_tick_frame(planes, page)
                enh["prev"] = None
                enh["next_tick"] = None
                return
            enh["last_cur"] = cur                                # smooth-tx fade base = the last wide gameplay frame
            if not settings["interpolation"] and not settings["smooth_camera"]:
                n += 1                                          # WIDESCREEN (no interp / smooth cam): one composed
                present(_snow_over(compose(cur, None, 1.0)), args.fps,   # (wide) frame per tick, faithful pacing
                        None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")
                enh["prev"] = cur                               # keep the pair warm for a live interp toggle-on
                enh["next_tick"] = None
                return
            if (enh["prev"] is not None
                    and enh["prev"].background_rgb.shape != cur.background_rgb.shape):
                enh["prev"] = None                              # widescreen width changed (resize/toggle) -> snap
            now = perf_counter()
            if enh["next_tick"] is None or enh["next_tick"] < now - 0.25:   # (re)sync after start/pause/menu
                enh["next_tick"] = now
            if enh["prev"] is None:                             # no pair yet -> the composed frame, unlerped
                present(_snow_over(_crop(compose(cur, None, 1.0))), args.fps)
                enh["prev"] = cur
                enh["next_tick"] += tick_dt
                return
            n += 1
            while True:                                         # >=1 frame per tick, then up to the due time
                now = perf_counter()
                alpha = min(1.0, max(0.0, 1.0 - (enh["next_tick"] - now) * TICK_HZ))
                pcam = _smooth_cam(cur, alpha)
                ref["last_pcam"] = pcam                          # so a transition can freeze at the eased camera
                present(_snow_over(_crop(compose(cur, enh["prev"], alpha, present_cam=pcam, crop=m_extra,
                                                 shake=cur.row_factor))),   # camera-shake jolt ([0x6BF8])
                        present_hz(),
                        None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")
                if perf_counter() >= enh["next_tick"]:
                    break
                pump()                                          # stay responsive between ticks (F10/resize/quit;
                if not ref["running"]:                          #  a menu stall resyncs via the 0.25s clause)
                    break
            enh["prev"] = cur
            enh["next_tick"] += tick_dt

        def present_smooth_tx_run(state, dos, run):
            """Present a SMOOTH FULL-WIDTH, FRAME-RATE-INDEPENDENT cave/death transition. ``run`` is the whole
            transition (already drained from native_frame_step_tagged, so ``state`` is now at the destination):
            its ``tx`` phases decide fade -> black -> curtain. The fade dims the last wide gameplay/bounce frame
            (enh.last_cur); the reveal wipes in the freshly-composed wide destination room. Each phase runs by
            wall clock at the display rate (not the source step count)."""
            from pre2.enhanced.compositor import compose
            from pre2.enhanced.transitions import apply_vfade
            enh["prev"] = enh["next_tick"] = None               # a transition breaks the interpolation pair
            kinds = {t[3][0] for t in run if t[3] is not None}
            # The fade base = what was LAST on screen. With the smooth camera that frame sits at the eased
            # (scam) position, not the DOS camera, so re-composing enh.last_cur (DOS-centred) would JUMP the
            # view as the fade starts. ref["last"] IS the last displayed frame (cropped, at scam, right width)
            # — use it directly. (Non-smooth: keep the recompose; ref["last"] would be an interp subframe.)
            neww = compose_wide_now(state, dos)                 # state is at the arrival/checkpoint now
            new = neww[0] if neww is not None else None
            if settings["smooth_camera"] and ref.get("last") is not None \
                    and new is not None and np.asarray(ref["last"]).shape == new.shape:
                old = np.asarray(ref["last"], np.uint8)
            else:
                old = compose(enh["last_cur"], None, 1.0) if enh["last_cur"] is not None else None
                if old is not None and new is not None and old.shape[1] > new.shape[1]:
                    d = (old.shape[1] - new.shape[1]) // 2      # smooth-camera stash carries the CROP margin ->
                    old = np.ascontiguousarray(old[:, d:d + new.shape[1]])   # crop the fade base to display width
            if "fade" in kinds and old is not None:
                _animate(state, _FADE_S, lambda p: apply_vfade(
                    old.copy(), int(_VFADE_MID * p), int(2 * _VFADE_MID - _VFADE_MID * p)))
            if "black" in kinds:
                base = old if old is not None else new
                if base is not None:
                    def _black(p):
                        fr = base.copy(); fr[:VIEWPORT_H] = 0
                        return fr
                    _animate(state, _CAVE_BLACK_S, _black)
            if "reveal" in kinds:
                base = new if new is not None else old
                if base is not None:
                    _animate(state, _REVEAL_S, lambda p: _curtain_frame(base, p))

        ref["p_prev"] = True    # a Start/P still held from the menu-in must not read as an instant pause edge
        while ref["running"]:
            pump()
            pause_check(state)                                     # [asm 6294] P freezes here until P resumes
            if ref["switch_level"] is not None:                    # Develop tab: jump/restart (a --debug cheat)
                lvl = ref["switch_level"]
                ref["switch_level"] = None
                lvl = state.data[DS + 0x2D8A] if lvl == "restart" else lvl
                print(f"menu: switching to level id {lvl:#04x}")
                state = native_cold_boot(gr, level=lvl)
                native_load_level_palette(state, dos)
                from pre2.native.audio import native_level_song_name, native_load_song
                native_load_song(state, native_level_song_name(state), gr)   # the NEW level's song (else the
                #                                                              previous level's music keeps playing)
                reveal_level(state, dos)
            if args.debug and settings["god"]:                     # Develop tab: keep the energy topped up
                state.data[DS + 0x27D6] = 3                        # [asm 52a8] full hearts, refreshed pre-tick
            drive_input(state)
            # WIDESCREEN ZONES: widen the state-level projection culls so entities go LIVE across the margins (one
            # system, no frozen re-projection + no active<->inactive gap at the 320 edge) instead of only the 320
            # view. Enabled only when TRUE widescreen actually draws the margin; _margin_for=0 on excluded levels
            # (auto-faithful there). ITEMS (8922 float pickups/popups) don't affect gameplay -> on with true
            # widescreen directly. ENEMIES (8022, shared with the object walker) DO -> gated behind the Active-zone
            # toggle. Both in tiles, per gameplay frame; 0 = faithful for every test + the plain runtime.
            # The item zone must cover the true-widescreen VISIBLE window, whose margin SLIDES near a world edge
            # (one side shrinks to 0, the other grows to 2*margin). Mirror the extract's slide (extract.py
            # _WORLD_W_PX=0x1000) so the zone is asymmetric = the exact visible window -> every visible item is a
            # live render-slot item (no frozen re-projection) WITHOUT wasting the 20-slot budget on off-screen
            # over-projection. +1 tile of slack for the mid-tick camera phase.
            _wm = (_margin_for(state)
                   if (settings["true_widescreen"] and settings["widescreen"] and not _room_locked(state)) else 0)
            if _wm:
                _cpx = (state.data[DS + 0x2DE4] | (state.data[DS + 0x2DE5] << 8)) * 16   # camera X in px
                _ml = min(_wm, _cpx)
                _ml = max(_ml, 2 * _wm - max(0, 0x1000 - (_cpx + 320)))
                _ml = min(max(_ml, 0), 2 * _wm)
                _mr = 2 * _wm - _ml
                set_item_zone_margins((_ml + 15) // 16 + 1, (_mr + 15) // 16 + 1)
                # enemies (opt-in): a symmetric 2*margin zone is fine -- the object pool has 64 slots, not 20.
                set_active_zone_margin(((2 * _wm + 15) // 16 + 1) if settings.get("widescreen_active_zone") else 0)
            else:
                set_item_zone_margins(0, 0)
                set_active_zone_margin(0)
            disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)

            def stream_frame(planes, page):
                """One faithful frame of a (possibly multi-frame) tick — today's path, per-frame audio."""
                present_tick_frame(planes, page)
                pump()
                if native_audio is not None:
                    native_audio.poll(state)                   # PER FRAME: a transition (death fly-off) yields
                    #   dozens of frames in ONE step — its queued sfx (the death SCREAM at the bounce start,
                    #   509d/50a6) must sound AT that frame, not after the whole animation
                return ref["running"]

            try:
                # Per-frame tag decides interpolation: an ``interpolatable`` frame (the normal tick AND every
                # death-BOUNCE frame — object motion) lerps; a transition frame (cave curtain, death fade +
                # checkpoint curtain, scene) streams 1:1 and breaks the lerp pair. So the parabolic death arc
                # smooths out (each of its 60 frames is a lerp tick) while wipes stay faithful.
                # Some levels ALWAYS render faithfully (no enhanced compose / interpolation / smooth transitions):
                # LEVEL A (0x09) is the gorilla boss whose fight only plays right in the plain 4:3 pipeline
                # (widescreen also bleeds its boss-face tiles). Matches the verified-working non-widescreen case.
                enhance_ok = state.data[DS + 0x2D8A] not in _WS_EXCLUDE_LEVELS
                # When the enhanced compositor will present the normal tick, its own compose rebuilds the frame,
                # so native_frame_step_tagged can SKIP the ~7ms faithful raster for that tick (the biggest per-tick
                # cost). Transitions + the faithful fallback still raster.
                want_enhanced = enhance_ok and (settings["interpolation"] or settings["widescreen"]
                                                or settings["smooth_camera"])
                _it = iter(native_frame_step_tagged(state, dos, disp, game_root=gr, raster_normal=not want_enhanced))
                for planes, page, interp_ok, tx in _it:
                    # A SMOOTH transition run: drain the whole transition (state advances to the destination),
                    # then present it present-time full-width (fade -> black -> curtain). The preceding bounce
                    # frames (interp) were already presented + stashed as the fade base (enh.last_cur).
                    if enhance_ok and _smooth_active() and tx is not None:
                        run = [(planes, page, interp_ok, tx)]
                        run.extend(_it)
                        present_smooth_tx_run(state, dos, run)
                        break
                    # Enhanced present path when interpolation OR widescreen is on (widescreen needs the composed
                    # wide frame even unlerped); a transition frame streams faithful 1:1 (smooth off) and breaks
                    # the lerp pair.
                    if enhance_ok and (settings["interpolation"] or settings["widescreen"]
                                       or settings["smooth_camera"]) and interp_ok:
                        present_interpolated(planes, page)
                        pump()
                        if native_audio is not None:
                            native_audio.poll(state)
                        if not ref["running"]:
                            break
                    else:
                        enh["prev"] = enh["next_tick"] = None   # transition (or interp off) -> break the lerp pair
                        if not stream_frame(planes, page):
                            break
                ref["tick_count"] += 1                         # one native_frame_step drive == one game tick
            except Pre2LevelEndTransition:
                between_levels(state, dos)                          # tally/carte flow, then the next level
            except Pre2CheatCredits:
                show_cheat_credits(state, dos)                     # dev-credits overlay, then resume this level
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
            transitions = 0
            i = 0
            while ref["running"] and i < gtd.n_ticks:
                pump()
                _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
                dpage = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)   # display PAGE (int) — NOT the
                #   `disp` Display object: this block runs in main()'s body, so binding `disp` here would shadow
                #   the Display and break blit_frame (disp.integer_scale) for the replay AND the live hand-over.
                try:
                    for planes, page in native_frame_step(state, dos, dpage, game_root=gr):
                        present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), args.fps,
                                f"PRE2 VM-less — tick replay {i}/{gtd.n_ticks}" if i % 20 == 0 else None)
                        pump()
                        if native_audio is not None:
                            native_audio.poll(state)           # per frame (death fly-off sfx timing)
                        if not ref["running"]:
                            break
                except Pre2LevelEndTransition:
                    # The recording is CONTINUOUS across the transition — record_from_vm captures every gameplay
                    # tick of the whole session, so gtd.keys[i:] (from THIS SAME index on) are the VM's recorded
                    # actions for the level it just warped/advanced INTO (e.g. a bonus-level warp), not a
                    # separate recording. Abandoning them here and handing off to blank live input was the
                    # actual bug (user: "it just ends in that bonus level") — keep injecting the remaining ticks
                    # instead.
                    #
                    # CRITICAL: the recorder's GAP_SITE hook only appends a (keys, digest) entry once DC1 has
                    # sampled keys THIS tick (rec["keys"] is not None) — so gtd.keys[i]/gtd.digests[i] are NOT
                    # "the last tick of the OLD level"; they are the FIRST tick recorded AFTER the load (the
                    # transition — tally/curtain/carte — runs with no gameplay ticks in between and produces NO
                    # entry of its own). So index i must be REPLAYED against the new, post-transition state, not
                    # skipped: do NOT advance i here — the next loop iteration re-injects gtd.keys[i] (unchanged)
                    # against the level the transition just loaded, which is exactly what produced gtd.digests[i]
                    # when this was recorded. (Verified: without this, EVERY demo that crosses a transition
                    # reports a spurious divergence at the very first post-transition tick.)
                    print(f"  tick replay: LEVEL END at tick {i} — continuing the recording into the next level")
                    between_levels(state, dos)
                    transitions += 1
                    continue
                except Pre2GameOverTransition:
                    print(f"  tick replay: GAME OVER at tick {i} — continuing the recording into the restart")
                    game_over_restart(state, dos)
                    transitions += 1
                    continue
                except Pre2GameComplete:
                    print(f"  tick replay: THE END at tick {i} — the game is finished")
                    the_end_restart(state, dos)
                    break
                except Exception as e:                             # noqa: BLE001
                    hold_last(f"tick replay gap at tick {i}/{gtd.n_ticks}: {type(e).__name__}: {str(e)[:70]}",
                              state)
                    pygame.quit()
                    return 0
                if div is None and gameplay_digest(state.data[DS:DS + 0x10000]) != gtd.digests[i]:
                    div = i
                    print(f"  tick replay DIVERGENCE at tick {i} (gameplay digest mismatch) — continuing")
                if native_audio is not None:
                    native_audio.poll(state)
                i += 1
            if div is None and i:
                print(f"  tick replay: {i}/{gtd.n_ticks} ticks reproduced byte-identically"
                      f"{f' across {transitions} level transition(s)' if transitions else ''} "
                      f"(digest matched every tick)")
            elif i:
                print(f"  tick replay: reached tick {i}/{gtd.n_ticks} ({transitions} level transition(s))")
            if ref["running"]:
                gameplay_loop(state, dos)                          # hand over to live play once the recording ends
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
        from pre2.native.audio import native_load_song, native_level_song_name
        native_load_song(state, native_level_song_name(state), gr)  # start the level music (the front-end normally does this at 01B7)
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
        from pre2.native.audio import native_load_song, native_level_song_name
        native_load_song(state, native_level_song_name(state), gr)  # start the level music (the front-end normally does this at 01B7)
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
            # front-end scenes are per-retrace (70Hz), but GAME-TICK-paced scenes run at the game rate
            # (args.fps ~23Hz) or they play ~3x too fast: the ATTRACT demo ([0x2879]=1, GAMEPLAY) and the
            # attract TITLE ANIMATION (scene.game_paced — the VM presents it via 44FB's 3-retrace 1C6F wait).
            fps = args.fps if (state.data[DS + 0x2879] == 1 or scene.game_paced) else _FRONT_END_FPS
            present_front_scene(scene, fps, "PRE2 VM-less — cold boot (front-end)")
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
