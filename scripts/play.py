"""Run Prehistorik 2 inside the DOS_RE VM.

This is intentionally a bootstrap/source-port runner, not a finished game
frontend.  It starts the original packed PRE2.EXE in pure original ASM (only the
LZEXE bootstrap accelerator and optional helpers are installed, never gameplay
hooks), and stops/snapshots/records at deterministic VM boundaries so we can begin
lifting the real game code from evidence.

PRE2 uses BIOS text, linear VGA, and a VGA/EGA-compatible 320x200 16-colour
planar graphics path.  The viewer renders those VM-visible video states and
plays the game's digital audio (MOD music + PCM SFX) via the emulated Sound
Blaster DMA path; PRE2 (GOG) is digital-only and never drives the OPL3/AdLib.

Three ways to use it:
  * ``--view``                live VGA/text viewer + digital audio; F11 records a
                              demo, F12 saves a snapshot.
  * ``--view --record-demo N`` start recording an input demo immediately.
  * ``--play-demo DIR``       replay a recorded demo (headless by default, or add
                              ``--view`` to watch it); deterministic, for testing.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.cpu import IF
from dos_re.input_demo import InputDemoPlayback, InputDemoRecorder, bios_key_value_from_scancode
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.keyboard import KeyDispatcher
from dos_re.snapshot import parse_addr, run_until, write_snapshot
from pre2.analysis import describe_exe, inventory_assets
from pre2.launch import build_command_tail
from dos_re.dosbox_savestate import is_dosbox_savestate
from pre2.runtime import create_pre2_runtime, load_dosbox_savestate, load_pre2_snapshot


def _default_snapshot_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / f"snapshot_pre2_{stamp}"


def _hook_group(name: str) -> str:
    """Collapse high-cardinality hook families so a one-line summary stays readable:
    per-asset SQZ decodes (MOTIF.SQZ, MAP.SQZ, ...) and per-type blits
    (sprite_blit_type0/1/11/...)."""
    import re
    if name.endswith(".SQZ"):
        return "sqz"
    m = re.match(r"(sprite_blit)_type\d+$", name)
    return m.group(1) if m else name


def _install_hook_trace(rt, args: argparse.Namespace) -> None:
    """For --trace-hooks: run the LIVE hybrid runtime and tally which recovered hooks fire,
    so you can watch coverage (and see where the game is still pure ASM). No oracle/diff."""
    if not getattr(args, "trace_hooks", False):
        return
    from pre2.checkpoints import enable_pre2_hook_trace
    stats = enable_pre2_hook_trace(rt)
    rt._trace_stats = stats

    def _summary(tag: str = "") -> None:
        # Cumulative per-hook totals — printed once at exit (the live view shows only the
        # current window).
        print(f"[hook-trace]{tag} total {stats.total()} fires | {stats.summary(group=_hook_group)}",
              flush=True)

    rt._verify_summary = _summary   # the loop prints a final summary on exit
    print("[hook-trace] live hybrid runtime — counting recovered hook fires (no oracle). "
          "Hooks absent here = still pure ASM.", flush=True)


def _install_verification_hooks(rt, args: argparse.Namespace) -> None:
    """Flip native replacement hooks into lockstep oracle mode for --verify-hooks.

    Normally the replacement hooks *are* the runtime (hybrid). With this flag the
    original ASM executes as the oracle instead, and each native result is diffed
    against it over the game-visible contract, printing OK/DIVERGENCE. Intended
    for offline replay of recorded demos/snapshots.
    """
    if not (getattr(args, "verify_hooks", False) or getattr(args, "full_verify", False)):
        return
    import re
    from time import perf_counter

    from pre2.checkpoints import enable_pre2_hook_verification

    def _group(name: str) -> str:
        # collapse the high-cardinality families so the summary stays one short line:
        # per-asset SQZ decodes (MOTIF.SQZ, MAP.SQZ, ...) and per-type blits
        # (sprite_blit_type0/1/11/...). Divergences still print the specific name.
        if name.endswith(".SQZ"):
            return "sqz"
        m = re.match(r"(sprite_blit)_type\d+$", name)
        return m.group(1) if m else name

    # Hooks fire thousands of times per frame (the audio mixer especially), so do
    # NOT print a line per OK. Default: print divergences the instant they happen,
    # plus a compact one-line cumulative summary at most every ~1.5s. --verify-verbose
    # restores the per-call OK stream.
    verbose = getattr(args, "verify_verbose", False)
    counts: dict[str, list[int]] = {}        # name -> [ok, diverged]
    divergences = [0]
    last = [perf_counter()]
    window_marker = [{}]                      # counts snapshot at the last periodic tick

    def _summary(tag: str = "", since: dict | None = None) -> None:
        # With ``since`` (a prior counts snapshot) show only the hooks checked in THIS window
        # — the live activity, not a growing cumulative list. Without it, the cumulative
        # totals (printed once at exit).
        grouped: dict[str, list[int]] = {}
        for name, c in counts.items():
            base = since.get(name, (0, 0)) if since is not None else (0, 0)
            ok, dv = c[0] - base[0], c[1] - base[1]
            if since is not None and ok == 0 and dv == 0:
                continue
            g = grouped.setdefault(_group(name), [0, 0])
            g[0] += ok
            g[1] += dv
        parts = " ".join(
            f"{n}={c[0]}" + (f"✗{c[1]}" if c[1] else "")
            for n, c in sorted(grouped.items(), key=lambda kv: -sum(kv[1]))
        ) or ("(idle)" if since is not None else "(no checks)")
        total = sum(c[0] + c[1] for c in grouped.values())
        flag = "OK" if divergences[0] == 0 else f"{divergences[0]} DIVERGENCE(S)"
        label = "now" if since is not None else "total"
        print(f"[verify-hooks]{tag} {label} {total} checks, {flag} | {parts}", flush=True)

    def _on_result(name: str, ok: bool, detail) -> None:
        c = counts.setdefault(name, [0, 0])
        if ok:
            c[0] += 1
            if verbose:
                print(f"[verify-hooks] OK          {name}", flush=True)
        else:
            c[1] += 1
            divergences[0] += 1
            print(f"[verify-hooks] DIVERGENCE  {name}: {detail}", flush=True)
        if not verbose:
            now = perf_counter()
            if now - last[0] >= 1.5:
                last[0] = now
                _summary(since=window_marker[0])
                window_marker[0] = {k: tuple(v) for k, v in counts.items()}

    rt._verify_summary = _summary  # let the caller print a final summary on exit
    mode = "per-call OK stream" if verbose else "divergences + periodic summary"
    if getattr(args, "full_verify", False):
        # Foolproof whole-memory audit: diffs the COMPLETE machine state after each
        # recovered routine vs the ASM (no hand-picked contract -> nothing leaks).
        # ~10x slower than --verify-hooks (re-runs the ASM routine + a full-memory
        # copy per call), so it is an offline snapshot/demo audit, not a live mode.
        from pre2.checkpoints.full_verify import enable_pre2_full_state_verify
        enable_pre2_full_state_verify(rt, on_result=_on_result)
        print(f"[verify-hooks] FULL-STATE oracle active (whole-memory diff; {mode})", flush=True)
    else:
        enable_pre2_hook_verification(rt, on_result=_on_result)
        print(f"[verify-hooks] lockstep oracle active vs original ASM (contract; {mode})", flush=True)


def _make_runtime(args: argparse.Namespace, *, fast_adlib: bool | None = None):
    exe = Path(args.exe)
    game_root = Path(args.game_root)
    command_tail = build_command_tail(args.dos_args)
    if fast_adlib is None:
        fast_adlib = bool(getattr(args, "fast_adlib", False))
    # The recovered/hybrid hooks + bridges are keyed to a specific build's memory
    # layout (code offsets + data segment). On a build they weren't derived against
    # (e.g. a different release), they fire on the wrong instructions and corrupt
    # execution — run the pure VM oracle instead with --no-replacements.
    native = not bool(getattr(args, "no_replacements", False))
    if args.snapshot:
        if is_dosbox_savestate(args.snapshot):
            # A DOSBox-X .sav: load its memory + CPU state (runs pure ASM — the
            # recovered hooks are keyed to our load segment, not DOSBox's).
            return load_dosbox_savestate(exe, args.snapshot, game_root=game_root, fast_adlib=fast_adlib)
        return load_pre2_snapshot(exe, args.snapshot, game_root=game_root,
                                  fast_adlib=fast_adlib, native_replacements=native)
    return create_pre2_runtime(exe, game_root=game_root, command_tail=command_tail,
                               fast_adlib=fast_adlib, native_replacements=native)


def _pygame_scan_map(pygame) -> dict[int, tuple[int, int]]:
    """Return pygame key -> (XT scan, ASCII) for the common PRE2 keys."""
    names = {
        "escape": (0x01, 0x1B), "return": (0x1C, 0x0D), "enter": (0x1C, 0x0D),
        "space": (0x39, 0x20), "up": (0x48, 0), "down": (0x50, 0),
        "left": (0x4B, 0), "right": (0x4D, 0),
        "left ctrl": (0x1D, 0), "right ctrl": (0x1D, 0),
        "left alt": (0x38, 0), "right alt": (0x38, 0),
    }
    for i, ch in enumerate("1234567890"):
        names[ch] = (0x02 + i, ord(ch))
    for i, ch in enumerate("qwertyuiop"):
        names[ch] = (0x10 + i, ord(ch))
    for i, ch in enumerate("asdfghjkl"):
        names[ch] = (0x1E + i, ord(ch))
    for i, ch in enumerate("zxcvbnm"):
        names[ch] = (0x2C + i, ord(ch))
    out = {}
    for name, value in names.items():
        try:
            out[pygame.key.key_code(name)] = value
        except Exception:
            pass
    return out


def _demo_metadata(args: argparse.Namespace, *, fast_adlib: bool) -> dict[str, object]:
    """Reproducibility knobs a replay must match to stay deterministic."""
    return {
        "game": "pre2",
        "exe": str(Path(args.exe).name),
        "command_tail": args.dos_args,
        "chunk_steps": int(args.chunk_steps),
        "present_hz": int(args.present_hz),
        "retrace_pulse": float(args.retrace_pulse),
        "timer_irq": bool(args.timer_irq),
        "fast_adlib": bool(fast_adlib),
        "input_irq_steps": int(args.input_irq_steps),
    }


def _present_surface(pygame, np, screen, rgb):
    h, w = rgb.shape[:2]
    surf = pygame.image.frombuffer(np.ascontiguousarray(rgb).tobytes(), (w, h), "RGB")
    win_w, win_h = screen.get_size()
    fit = max(1, min(win_w // w, win_h // h))
    target = (w * fit, h * fit)
    if fit != 1:
        surf = pygame.transform.scale(surf, target)
    screen.fill((0, 0, 0))
    screen.blit(surf, ((win_w - target[0]) // 2, (win_h - target[1]) // 2))
    pygame.display.flip()


def _pump_and_step(rt, *, now, pic, sound_blaster, timer_irq, input_irq_steps, tick_state, n_steps):
    """One sub-batch: raise due PIT/SB IRQs against the clock value ``now``, deliver
    pending IRQs (IF-gated), then run ``n_steps`` CPU instructions.

    Shared by live play (``now`` = wall clock) and the demo record/replay path
    (``now`` = the deterministic emulated clock).  Driving the timer from ``now``
    rather than a fixed one-tick-per-frame is what makes a demo run at the game's
    real PIT rate (and lets the Sound Blaster stream) instead of in slow motion.
    """
    if timer_irq:
        tick_period = 1.0 / max(1.0, rt.dos.pit_channel0_hz())
        while now >= tick_state["next"]:
            if pic is not None:
                pic.raise_irq(0)
            elif rt.cpu.get_flag(IF):
                deliver_interrupt(rt, 0x08, max_steps=input_irq_steps)
            tick_state["next"] += tick_period
            if tick_state["next"] < now - 0.25:          # fell far behind: resync
                tick_state["next"] = now + tick_period
    if sound_blaster is not None:
        sound_blaster.service()
    if pic is not None:                                  # deliver pending IRQs (IF-gated)
        guard = 0
        while rt.cpu.get_flag(IF) and guard < 64:
            n = pic.acknowledge()
            if n is None:
                break
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=input_irq_steps)
            guard += 1
    for _ in range(n_steps):
        rt.cpu.step()


def _advance_demo_frame(rt, *, chunk_steps, sub_batch, clock, pic,
                        sound_blaster, timer_irq, input_irq_steps, tick_state):
    """Advance exactly one demo frame on the deterministic emulated clock.

    ``clock`` is the emulated time source — a function of ``cpu.instruction_count``
    (so it advances every *instruction*, including inside tight loops). That makes
    the whole frame -> VM-state mapping a pure function of the instruction stream (a
    recorded demo replays identically) yet lets the PIT, Sound Blaster AND port-based
    busy-waits (e.g. the VGA 0x3DA vertical-retrace poll) all see time advance — a
    per-frame or per-sub-batch clock freezes those polls and the game hangs.
    """
    remaining = chunk_steps
    while remaining > 0:
        n = min(sub_batch, remaining)
        _pump_and_step(rt, now=clock(), pic=pic, sound_blaster=sound_blaster,
                       timer_irq=timer_irq, input_irq_steps=input_irq_steps,
                       tick_state=tick_state, n_steps=n)
        remaining -= n


def _run_view(rt, args: argparse.Namespace, *, playback: InputDemoPlayback | None = None) -> int:
    """Live VGA/text viewer for PRE2 bring-up, with digital audio and demo record/replay.

    This intentionally avoids gameplay hooks/frame boundaries.  It advances a
    fixed ``chunk_steps`` of original VM instructions per displayed frame, then
    presents whatever the emulated VGA/text hardware exposes.  The fixed step
    budget per frame is what makes the frame counter a deterministic demo clock:
    a recorded demo replays identically as long as ``chunk_steps``/``timer_irq``/
    ``fast_adlib`` match (they are stored in the demo manifest).
    """
    import pygame
    import numpy as np
    from time import perf_counter, sleep
    from sdl_view import (SoundBlasterAudio, render_planar_rgb, render_planar_rgb_from_planes,
                          render_text_rgb, render_vga_rgb)
    from pre2.bridge.game_visual_state import capture_game_visual_state, render_game_visual_state
    from pre2.bridge.live_render import compose_curtain_planes, compose_vfade_planes, render_visual_planes
    from pre2.bridge.particles import read_particles
    from pre2.bridge.foreground_tiles import read_foreground_state
    from pre2.bridge.gameplay_effects import apply_gameplay_effects, capture_gameplay_effects
    from pre2.bridge.gameover_scene import build_gameover_scene, load_gameover_asset
    from pre2.bridge.tally_scene import build_tally_scene
    from pre2.bridge.tally_panel import read_tally_panel
    from pre2.bridge.image_scene import identify_image, render_image_scene
    from pre2.bridge.scene_state import derive_scene_kind
    from pre2.recovered.gameover_background import render_gameover_background
    from pre2.recovered.scene_compositor import RecoveredBackground
    from pre2.recovered.faithful_visual import FaithfulVisualGap, SceneKind
    from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
    from dos_re.cpu import HaltExecution, UnsupportedInstruction, IF
    from dos_re.dos import ConsoleInputWouldBlock
    from dos_re.runtime import enable_sound_blaster

    replaying = playback is not None
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=1024)
    pygame.init()
    scale = max(1, int(args.scale))
    screen = pygame.display.set_mode((640 * scale, 400 * scale), pygame.RESIZABLE)
    pygame.display.set_caption("PRE2 DOS_RE VM - starting")
    scan_map = _pygame_scan_map(pygame)
    clock = pygame.time.Clock()
    steps_done = 0
    frame = 0
    running = True

    # Real-time pacing for live play: model the PIT (the program programmed ch0
    # itself) and the 70 Hz VGA retrace on the wall clock, and let the game's own
    # timer/vsync waits set the speed — no per-game constants.  Demo record/replay
    # keep the deterministic fixed-chunk clock so recordings stay reproducible.
    present_period = 1.0 / max(1, int(args.present_hz))
    # Narrow/widen the emulated VGA vertical-retrace pulse (live only). A realistic narrow
    # pulse gates the mode-select scroll's "wait until retrace set" half-wait to one frame
    # per 70Hz refresh; the legacy 0.28 lets it run ~2x fast on a fast host.
    rt.dos.vga_retrace_active_fraction = float(args.retrace_pulse)
    # Optional era-style instruction-rate ceiling for live play (0 = unlimited). Capped per
    # displayed frame; the VM stops stepping once it spends the budget and waits out the
    # frame (still pumping audio), so an ungated busy-loop can't run away on a fast host.
    live_cpu_budget = (int(args.cpu_hz) // max(1, int(args.present_hz))) if int(args.cpu_hz) > 0 else None
    realtime_batch = 2000  # demo sub_batch: IRQ-delivery boundary (fixed for replay determinism)
    # Live play services the emulated Sound Blaster's block IRQ only at batch
    # boundaries, so a large batch delivers IRQ7 late and stretches each ~20 ms DMA
    # block -> the live audio underruns (measured: 2000 steps => only 84% of the PCM
    # rate produced).  A small batch checks the SB clock often enough to keep blocks
    # on time (~97%) with no measurable cost to the game frame-rate.  Demo replay is
    # unaffected: it keeps `realtime_batch` above so recordings stay reproducible.
    live_irq_batch = 256
    status = "replaying" if replaying else "running"
    rt.cpu.trace_enabled = False
    rt.dos.console_input_fallback = None

    # PRE2 (GOG) is digital-only: it detects the Sound Blaster and streams MOD music +
    # PCM SFX via DMA; it never writes the OPL3/AdLib (YM3812) ports (verified: 0 OPL
    # writes during gameplay), so there is no FM path here.
    audio_status: dict[str, str] = {}

    # Gameplay digital audio: enable the emulated Sound Blaster so the original
    # driver detects it and DMA-streams its PCM (MOD music + PCM SFX).  Enabled for
    # live play AND demo record/replay — the demo path drives the SB block IRQ from
    # the deterministic emulated clock (below) so demos sound and run like live play
    # while staying reproducible.  IRQ0/IRQ7 are delivered at batch boundaries.
    sb_audio = None
    sound_blaster = None
    audio_poll = None
    audio_mode = getattr(args, "audio", "adlib")
    if audio_mode != "off":
        # The game must detect a digital device to run its song-loader / play-SFX commands
        # (which we observe). For the ENHANCED path the audio is produced by the recovered
        # native system, so we attach only a DETECTION STUB: the game detects the card and
        # emits commands, but no PCM streams and no playback IRQ fires (the SB/DMA/IRQ block
        # production is gone — oracle/scaffolding only). The ADLIB path plays the SB's own
        # PCM, so it needs the full streaming card.
        sound_blaster = enable_sound_blaster(rt, detection_only=(audio_mode == "enhanced"))
        if audio_mode == "enhanced":
            # Modern path: observe the recovered audio *commands* and render them through
            # the recovered native audio system (the SB is a detection stub; its PCM/IRQ
            # block production is gone).
            from sdl_view import SdlEnhancedAudio
            from pre2.bridge.audio_commands import install_command_observers
            # With the detection stub the game's audio ISR does not run during playback (no
            # block IRQ fires), so the recovered tracker/mixer checkpoints would never be hit
            # anyway; drop them so the lone detection-handshake IRQ can't touch the recovered
            # mix_channel either (its live state divergence once corrupted channel state into
            # a freeze). Kept under --verify-hooks (verification uses the ASM result).
            if not getattr(args, "verify_hooks", False):
                for _addr in ((0x1030, 0x227C), (0x1030, 0x218F)):   # tracker, mixer
                    rt.cpu.replacement_hooks.pop(_addr, None)
                    rt.cpu.hook_names.pop(_addr, None)
            # Live enhanced audio is a COMMAND-DRIVEN modern player with its OWN continuous
            # clock. The recovery layer discovers intent (which song / SFX); SDL_mixer then
            # plays the whole identified .TRK module on its own C audio thread. Music tempo is
            # owned by the audio device, so it can NOT be slowed by Python/VM/render/frame
            # scheduling, queue starvation, or any SB/DMA/IRQ cadence (the live clocking bug).
            # The recovered tracker/mixer stay on the faithful oracle path; enhanced never
            # consumes original PCM or recovered mix timing.
            sb_audio = SdlEnhancedAudio(pygame, args.game_root, audio_status)
            audio_poll = install_command_observers(rt.cpu, sb_audio.post, args.game_root)
        else:
            sb_audio = SoundBlasterAudio(pygame, sound_blaster, audio_status)

    # The deterministic demo clock: advanced a fixed present_period each frame so the
    # PIT/SB/retrace cadence is a pure function of the frame index (reproducible).
    # The clock source (this vs. perf_counter) is chosen per frame from `realtime`.
    # The deterministic demo clock advances with cpu.instruction_count (so it ticks
    # every instruction, even inside tight port-poll loops); `base` re-anchors it
    # when the source switches so it stays continuous.
    det_speed = max(1, int(args.chunk_steps) * max(1, int(args.present_hz)))
    vclock = {"base": perf_counter()}
    tick_state = {"next": perf_counter()}
    prev_realtime = None
    det_now = lambda: vclock["base"] + rt.cpu.instruction_count / det_speed  # noqa: E731
    # Demo (deterministic) presentation pacing: each game-frame is a fixed slice of
    # *game* time (present_period), so we pace game-frames to the wall clock for
    # real-time speed, and DECOUPLE the (expensive) screen render — drawing only at
    # ~present_hz and skipping it when behind so the VM/audio keep real-time instead
    # of the whole loop collapsing to the render rate.  (Replay stays bit-identical:
    # this only changes when we *draw*, never the VM advance.)
    sim_deadline = perf_counter()
    last_render = 0.0
    last_trace = 0.0   # throttle for the --trace-hooks periodic per-hook tally
    trace_marker = {}  # --trace-hooks window boundary: counts at the last periodic tick
    last_audio = 0.0   # throttle for in-loop audio pumping (live play)

    demo: dict[str, InputDemoRecorder | None] = {"rec": None}
    fast_adlib = bool(getattr(args, "fast_adlib", False))

    def start_recording(name: str) -> None:
        rec = InputDemoRecorder(root=Path(args.demo_dir), name=name, metadata=_demo_metadata(args, fast_adlib=fast_adlib))
        out = rec.start(rt, boundary=frame)
        demo["rec"] = rec
        print(f"recording demo -> {out}")

    def stop_recording() -> None:
        rec = demo["rec"]
        if rec is not None and rec.active:
            out = rec.stop(boundary=frame)
            print(f"saved demo ({rec.event_count} events) -> {out}")
        demo["rec"] = None

    def deliver_input(scancode: int) -> None:
        nonlocal status
        try:
            deliver_scancode(rt, scancode, max_steps=args.input_irq_steps)
        except Exception as exc:  # noqa: BLE001 - keep viewer alive during RE
            status = f"keyboard interrupt failed: {type(exc).__name__}: {exc}"
            return
        rec = demo["rec"]
        if rec is not None and rec.active:
            rec.record_scan(boundary=frame, scancode=scancode)

    key_dispatcher = KeyDispatcher(deliver_input)
    pending_dos: list[tuple[int, str]] = []

    if not replaying and args.record_demo:
        start_recording(args.record_demo)

    def flush_dos_keys() -> None:
        rec = demo["rec"]
        while pending_dos:
            sc, text = pending_dos.pop(0)
            value = bios_key_value_from_scancode(sc, text)
            if value is None:
                continue
            rt.dos.key_queue.append(value)
            if rec is not None and rec.active:
                rec.record_dos_key(boundary=frame, scancode=sc, text=text, value=value)

    last_rgb = [None]  # most recent rendered frame, for F10 screenshots
    faithful_info = [""]  # title-bar note for the live faithful renderer (gameplay only)
    faithful = getattr(args, "faithful", False)
    faithful_verify = getattr(args, "faithful_verify", False)

    gap_seen = [None]
    boundary_capture = [None]  # (rgb, page, scene_kind_name, verify_Δ|None) from the last 6772 commit
    curtain_cache = [None]     # new-room planes (at src page) rendered once per curtain reveal (3054)
    last_committed = [None]    # (planes, page) of the last 6772 frame — base for the vertical fade-out
    particle_frame = [None]    # ParticleFrame snapshotted at 4b8e entry (one-shot; gone by 6772)
    foreground_frame = [None]  # ForegroundState snapshotted at 3732 entry (active list cleared by 6772)
    gameover_pending = [None]  # (scroll, page) stashed at the 9C87 diorama present (scroll inc's after)
    tally_pending = [None]     # TallyPanelInputs stashed at the 51A3 driver (the % counts up before the flip)
    scene_capture = [None]     # (rgb, page, ic, label) of the last complete recovered SCENE frame (at the flip)
    current_13h_image = [None]  # (asset name, has_logo) of the mode-13h image on screen; set at 91C0/9090
    last_capture_ic = [0]      # instruction count at the last 6772 capture (staleness for the death spin)
    last_hud = [None]          # (4 HUD-strip plane slices) from the last 6772 commit — the DISPLAYED HUD
    last_gp_ic = [0]           # instruction count when a GAMEPLAY/IRIS frame was last DISPLAYED
    _DSEG = 0x1A0F
    _HUD_OFF = 176 * 0x28      # HUD strip start within a page (row 176)
    _HUD_LEN = 24 * 0x28       # rows 176..199 (status bar + dynamic glyphs)

    def _snapshot_hud(planes, page):
        o = (page + _HUD_OFF) & 0xFFFF
        return [bytes(planes[p][o:o + _HUD_LEN]) for p in range(4)]

    def _overlay_hud(planes, page, hud):
        o = (page + _HUD_OFF) & 0xFFFF
        for p in range(4):
            planes[p][o:o + _HUD_LEN] = hud[p]

    if faithful:
        # Capture the GameVisualState at the frame-commit boundary 1030:6772 (palette-fade entry, POST
        # page-flip): there ega_display_start IS the just-committed frame and the scroll/camera state has
        # not yet advanced, so render_frame(state)@display_start == display_start (proven Δ≈0). The viewer
        # then mirrors the CAPTURED committed frame, not an ad-hoc live read (which describes the back
        # buffer being built -> the camera/page mismatch). render_game_visual_state reuses render_visual
        # -> the same recovered leaves (one-impl). Wrap the existing palette-fade hook at 6772.
        rt.cpu.pre2_dos = rt.dos
        _BND = (0x1030, 0x6772)
        _orig6772 = rt.cpu.replacement_hooks.get(_BND)

        def _capture_at_boundary(c):
            try:
                disp = rt.program.memory.ega_display_start
                # The effect overlays (4b8e particles + 3721 foreground tiles, stashed at their own pass
                # entries; 54AB fireflies read live) compose into the GameVisualState so the canonical
                # render_game_visual_state yields the COMPLETE displayed gameplay frame.
                fx = capture_gameplay_effects(c.mem, particle_frame=particle_frame[0],
                                              foreground_frame=foreground_frame[0])
                gvs = capture_game_visual_state(c.mem, c.pre2_dos, disp,
                                                game_root=args.game_root, effects=fx)
                planes, page = render_game_visual_state(gvs)       # raises FaithfulVisualGap for scenes
                d = None
                if faithful_verify:
                    data = rt.program.memory.data; d = 0
                    for p in range(4):
                        apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
                        for o in range(176 * 0x28):                # gameplay viewport (HUD verified separately)
                            a = (page + o) & 0xFFFF
                            if planes[p][a] != data[apb + a]:
                                d += 1
                boundary_capture[0] = (render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette),
                                       page, gvs.scene_kind.name, d)
                last_committed[0] = (planes, page)  # base for the vertical fade-out (the frame it clears)
                last_capture_ic[0] = rt.cpu.instruction_count
                last_hud[0] = _snapshot_hud(planes, page)  # the DISPLAYED HUD (frozen between commits)
            except FaithfulVisualGap:
                boundary_capture[0] = None         # a SCENE/IMAGE frame at 6772 -> handled at present time
            except Exception:
                boundary_capture[0] = None
            curtain_cache[0] = None                # the per-frame boundary ends any curtain in progress
            particle_frame[0] = None               # consumed for this frame; 4b8e re-stashes next frame
            foreground_frame[0] = None             # consumed; 3732 re-stashes next frame it runs
            if _orig6772 is not None:
                return _orig6772(c)
            interpret_current_instruction_without_hook(c)          # no palette hook -> run the ASM instr

        rt.cpu.replacement_hooks[_BND] = _capture_at_boundary
        rt.cpu.hook_names[_BND] = "palette_fade+faithful_capture"

        # The page-flip CURTAIN (1030:3054) runs in a BLOCKING vsync-paced sub-loop that never reaches
        # the 6772 boundary, so the viewer would freeze on the last capture and "teleport" past the
        # reveal. Capture the partial reveal at each curtain step (307D, after both strips of the
        # iteration): render the new room ONCE (cached), then reveal `completed_pairs` strip-pairs over
        # black via the recovered panel_copy (compose_curtain_planes) — proven byte-exact vs the ASM
        # displayed page (pre2/probes/verify_curtain.py). No ASM VRAM.
        _CURTAIN = (0x1030, 0x307D)
        _orig307d = rt.cpu.replacement_hooks.get(_CURTAIN)

        def _rw(mem, off):
            b = ((_DSEG << 4) + off) & 0xFFFFF
            return mem.data[b] | (mem.data[b + 1] << 8)

        def _capture_curtain_step(c):
            try:
                src = _rw(c.mem, 0x2DD8)
                dst = _rw(c.mem, 0x2DD6)
                step = c.mem.data[(0x1030 << 4) + 0x3050] | (c.mem.data[(0x1030 << 4) + 0x3051] << 8)
                completed = step // 4 + 1                       # strip-pairs done by this 307D
                if curtain_cache[0] is None:
                    nr, _, kind = render_visual_planes(c.mem, c.pre2_dos, game_root=args.game_root,
                                                       display_page=src)
                    curtain_cache[0] = (nr, src, kind.name)
                nr, csrc, kindname = curtain_cache[0]
                planes, page = compose_curtain_planes(nr, csrc, dst, completed)
                # The engine's curtain only copies the VIEWPORT rows, leaving the HUD strip on the dst
                # page persisting from before (buffer persistence) -> the HUD does NOT go black during
                # the reveal. compose_curtain_planes starts from a fully-black base, so restore the HUD
                # strip from the last committed frame to match.
                if last_hud[0] is not None:
                    _overlay_hud(planes, page, last_hud[0])
                boundary_capture[0] = (
                    render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette),
                    page, kindname, None)
            except Exception:
                pass
            if _orig307d is not None:
                return _orig307d(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_CURTAIN] = _capture_curtain_step
        rt.cpu.hook_names[_CURTAIN] = "curtain_step+faithful_capture"

        # The VERTICAL fade-out (1030:30C6) is the other blocking vsync sub-loop: it clears the
        # displayed page to black in two full-width 10-row bands converging top+bottom toward the
        # middle. Capture each step (3111, after both bands of the iteration are cleared): take the LAST
        # committed frame (the frame it clears — re-rendering would give the wrong frame, the state has
        # moved on) and black the cleared rows via compose_vfade_planes — proven byte-exact vs the ASM
        # displayed page (pre2/probes/verify_vfade.py). No ASM VRAM.
        _VFADE = (0x1030, 0x3111)
        _orig3111 = rt.cpu.replacement_hooks.get(_VFADE)

        def _capture_vfade_step(c):
            try:
                if last_committed[0] is not None:
                    bplanes, bpage = last_committed[0]
                    page = _rw(c.mem, 0x2DD6)
                    s52 = c.mem.data[(0x1030 << 4) + 0x3052] | (c.mem.data[(0x1030 << 4) + 0x3053] << 8)
                    s50 = c.mem.data[(0x1030 << 4) + 0x3050] | (c.mem.data[(0x1030 << 4) + 0x3051] << 8)
                    top = (s52 - page) // 0x28 + 10            # top band accumulated extent
                    bot = (s52 + s50 - page) // 0x28           # bottom band start
                    planes, pg = compose_vfade_planes(bplanes, bpage, top, bot)
                    boundary_capture[0] = (
                        render_planar_rgb_from_planes(planes, pg, c.pre2_dos.vga_palette),
                        pg, "GAMEPLAY", None)
                    # When the bands meet (top>=bot) the page is fully black. The death issues TWO
                    # 30C6 clears on the SAME page; in the engine the 2nd clears an already-black page
                    # (invisible). Promote the black result to the fade base so a repeated 30C6 stays
                    # black instead of re-fading the (stale) level frame a second time.
                    if top >= bot:
                        last_committed[0] = (planes, pg)
            except Exception:
                pass
            if _orig3111 is not None:
                return _orig3111(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_VFADE] = _capture_vfade_step
        rt.cpu.hook_names[_VFADE] = "vfade_step+faithful_capture"

        # Point particles (1030:4B8E) are one-shot: 4b8e draws + KILLS each slot every frame, so the
        # array is empty by the 6772 commit. Snapshot it here at 4b8e ENTRY (pre-kill); the 6772 render
        # replays the draw via the recovered draw_particles (proven byte-exact, pre2/probes/verify_particles.py).
        _PARTS = (0x1030, 0x4B8E)
        _origparts = rt.cpu.replacement_hooks.get(_PARTS)

        def _capture_particles(c):
            try:
                pf = read_particles(c.mem)
                particle_frame[0] = pf if pf.particles else None
            except Exception:
                particle_frame[0] = None
            if _origparts is not None:
                return _origparts(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_PARTS] = _capture_particles
        rt.cpu.hook_names[_PARTS] = "particles_capture"

        # Foreground tiles (1030:3721 pass body 3732): the pass redraws flag-0x40 tiles OVER the sprites,
        # but it reads the active sprite list [0x4F0A], which the object pass rebuilds each frame -> by the
        # 6772 commit the list state no longer matches what was drawn. Snapshot the ForegroundState at the
        # 3732 pass entry (active list still populated); the 6772 render replays render_foreground_tiles
        # (proven byte-exact, pre2/probes/verify_foreground_tiles.py).
        _FGTILES = (0x1030, 0x3732)
        _origfg = rt.cpu.replacement_hooks.get(_FGTILES)

        def _capture_foreground(c):
            try:
                foreground_frame[0] = read_foreground_state(c.mem)
            except Exception:
                foreground_frame[0] = None
            if _origfg is not None:
                return _origfg(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_FGTILES] = _capture_foreground
        rt.cpu.hook_names[_FGTILES] = "foreground_capture"

        # GAME-OVER scene (SCENE kind, no 6772 boundary). Its non-gameplay loop runs the diorama present
        # (9C87) + object pass + page flip (44FB). Stash the scroll 9C87 used (the counter increments
        # after, at 9CCD), then at the flip render the COMPLETE recovered scene (RecoveredBackground from
        # GAMEOVER.SQZ + the object overlay), exactly the byte-exact path proven by verify_gameover_full.py.
        try:
            _go_asset = load_gameover_asset(args.game_root)
        except Exception:
            _go_asset = None
        _GO_PRESENT = (0x1030, 0x9C87)
        _origgo = rt.cpu.replacement_hooks.get(_GO_PRESENT)

        def _capture_gameover_present(c):
            try:
                scroll = c.mem.data[(0x1A0F << 4) + 0x6BC4]
                page = c.mem.data[(0x1A0F << 4) + 0x2DD8] | (c.mem.data[(0x1A0F << 4) + 0x2DD9] << 8)
                gameover_pending[0] = (scroll, page)
            except Exception:
                gameover_pending[0] = None
            if _origgo is not None:
                return _origgo(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_GO_PRESENT] = _capture_gameover_present
        rt.cpu.hook_names[_GO_PRESENT] = "gameover_present_capture"

        # TALLY scene (level-end): the same 0x2C loop + 44FB flip, but a BLACK bg + the text panel (51A3
        # driver). Mark the frame when the panel driver runs; the flip renders the recovered tally scene.
        _TALLY_DRIVER = (0x1030, 0x51A3)
        _origtally = rt.cpu.replacement_hooks.get(_TALLY_DRIVER)

        def _mark_tally(c):
            try:                                   # stash the panel state AS DRAWN (the % counts up after)
                tally_pending[0] = read_tally_panel(c.mem)
            except Exception:
                tally_pending[0] = None
            if _origtally is not None:
                return _origtally(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_TALLY_DRIVER] = _mark_tally
        rt.cpu.hook_names[_TALLY_DRIVER] = "tally_driver_mark"

        _GO_FLIP = (0x1030, 0x44FB)
        _origflip = rt.cpu.replacement_hooks.get(_GO_FLIP)

        def _capture_scene_flip(c):
            # At the page flip the back page holds a complete frame. Render the recovered SCENE for the
            # screen that drew it: game-over (9C87 diorama present ran) or tally (51A3 panel driver ran).
            try:
                if gameover_pending[0] is not None and _go_asset is not None:
                    scroll, page = gameover_pending[0]
                    bg = RecoveredBackground(tuple(bytes(pl) for pl in
                                                    render_gameover_background(_go_asset, scroll, page)))
                    planes, _st = build_gameover_scene(c.mem, rt.dos, game_root=args.game_root,
                                                       page=page, background=bg)
                    if last_hud[0] is not None:        # the displayed game-over HUD is FROZEN at death
                        _overlay_hud(planes, page, last_hud[0])
                    rgb = render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette)
                    scene_capture[0] = (rgb, page, rt.cpu.instruction_count, "GAMEOVER")
                elif tally_pending[0] is not None:
                    page = c.mem.data[(0x1A0F << 4) + 0x2DD8] | (c.mem.data[(0x1A0F << 4) + 0x2DD9] << 8)
                    planes, _st = build_tally_scene(c.mem, rt.dos, game_root=args.game_root, page=page,
                                                    panel_inputs=tally_pending[0])
                    rgb = render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette)
                    scene_capture[0] = (rgb, page, rt.cpu.instruction_count, "TALLY")
            except Exception:
                pass
            gameover_pending[0] = None
            tally_pending[0] = None
            if _origflip is not None:
                return _origflip(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_GO_FLIP] = _capture_scene_flip
        rt.cpu.hook_names[_GO_FLIP] = "scene_flip_capture"

        # Mode-13h IMAGE scenes (title/menu/titus/intro): identify which image is on screen at the 91C0
        # copy (fingerprint its source) so the faithful 13h path can re-render it from the recovered asset
        # instead of reading the A000 framebuffer.
        _IMG_COPY = (0x1030, 0x91C0)
        _origimg = rt.cpu.replacement_hooks.get(_IMG_COPY)

        def _identify_13h(c):
            try:
                src = ((c.s.ds << 4) + c.s.si) & 0xFFFFF
                name = identify_image(bytes(c.mem.data[src:src + 256]), args.game_root)
                if name is not None:
                    prev = current_13h_image[0]
                    # only reset the logo state when a DIFFERENT image loads; the bg copy re-runs each
                    # frame on the same title, but the logo (9090) is sticky on screen.
                    if prev is None or prev[0] != name:
                        current_13h_image[0] = (name, False)
            except Exception:
                pass
            if _origimg is not None:
                return _origimg(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_IMG_COPY] = _identify_13h
        rt.cpu.hook_names[_IMG_COPY] = "image13h_identify"

        # 90C0 = the title logo-top overlay copy (rep movsw, AFTER the background). Mark the logo present.
        _IMG_LOGO = (0x1030, 0x90C0)
        _origlogo = rt.cpu.replacement_hooks.get(_IMG_LOGO)

        def _mark_13h_logo(c):
            if current_13h_image[0] is not None:
                current_13h_image[0] = (current_13h_image[0][0], True)
            if _origlogo is not None:
                return _origlogo(c)
            interpret_current_instruction_without_hook(c)

        rt.cpu.replacement_hooks[_IMG_LOGO] = _mark_13h_logo
        rt.cpu.hook_names[_IMG_LOGO] = "image13h_logo"

    def _faithful_planar(mem_bytes, ds):
        """Mirror the committed frame from the 1030:6772 frame-boundary GameVisualState capture (NOT an
        ad-hoc live read — that describes the back buffer being built). Gameplay/iris frames come from the
        latest boundary capture; scenes whose leaf is not recovered yet fail LOUD (diagnostic frame +
        console hint), never ASM VRAM."""
        cur_kind = derive_scene_kind(rt.cpu.mem, rt.dos)
        if cur_kind in (SceneKind.GAMEPLAY, SceneKind.IRIS):
            # Long gap with no 6772 commit (e.g. the player-death fall: a sub-loop that animates the
            # player via the object system but never reaches 6772) would FREEZE the viewer on the last
            # capture. When the VM is idling in the per-frame governor spin (1C6F-1C7E) the displayed
            # frame IS committed + render-consistent (proven render_frame Δ=0 there), so render LIVE
            # instead of freezing. Gated on staleness so normal gameplay (gaps << a frame) always uses
            # the clean 6772 capture; the curtains idle in 3054/30C6 (handled by their own hooks), not here.
            # The threshold sits just above a normal per-frame gap (~17-24k) so it never fires in normal
            # play (which would double-render), but trips quickly into a death/transition pause -> minimal
            # initial freeze before the live render takes over.
            ip = rt.cpu.s.ip
            if (rt.cpu.instruction_count - last_capture_ic[0] > 30000
                    and (rt.cpu.s.cs & 0xFFFF) == 0x1030 and 0x1C6F <= ip <= 0x1C7E):
                try:
                    disp = rt.program.memory.ega_display_start
                    planes, page, k = render_visual_planes(rt.cpu.mem, rt.dos,
                                                           game_root=args.game_root, display_page=disp)
                    apply_gameplay_effects(planes, page, capture_gameplay_effects(
                        rt.cpu.mem, particle_frame=particle_frame[0],
                        foreground_frame=foreground_frame[0]))
                    # The DISPLAYED HUD lags the live state: it only changes when the engine redraws it
                    # AND flips the buffer, which doesn't happen during the death gap (no 6772). So at the
                    # moment of death the live HUD already shows the post-death lives, but the screen still
                    # shows the pre-death HUD until respawn. Freeze the HUD strip at the last committed
                    # value (instant death also doesn't reduce energy, so this matches).
                    if last_hud[0] is not None:
                        _overlay_hud(planes, page, last_hud[0])
                    rgb = render_planar_rgb_from_planes(planes, page, rt.dos.vga_palette)
                    # CACHE the live frame so off-governor refreshes (the death loop dips out of the
                    # 1C6x spin) keep showing the latest live frame instead of flickering back to the
                    # stale pre-death capture; also keep last_committed current as the vfade base.
                    boundary_capture[0] = (rgb, page, k.name, None)
                    last_committed[0] = (planes, page)
                    last_gp_ic[0] = rt.cpu.instruction_count
                    faithful_info[0] = f"faithful[{k.name}]@spin(live)"
                    return rgb
                except Exception:
                    pass
            cap = boundary_capture[0]
            if cap is not None and cap[2] in ("GAMEPLAY", "IRIS"):
                rgb, page, kindname, d = cap
                if faithful_verify and d is not None:
                    faithful_info[0] = f"faithful[{kindname}]@6772 Δ={d}" + ("" if d <= 96 else " !!")
                else:
                    faithful_info[0] = f"faithful[{kindname}]@6772"
                last_gp_ic[0] = rt.cpu.instruction_count
                return rgb
            faithful_info[0] = "faithful: awaiting 6772 boundary capture"
            return np.full((200, 320, 3), (48, 0, 32), dtype=np.uint8)
        # Recovered SCENE (game-over diorama / tally panel), captured at the page flip. Show it while its
        # capture is fresh (the 0x2C loop flips ~every frame).
        if scene_capture[0] is not None and rt.cpu.instruction_count - scene_capture[0][2] < 200000:
            faithful_info[0] = f"faithful[{scene_capture[0][3]}]@flip"
            return scene_capture[0][0]
        # SCENE / IMAGE. A brief blip to SCENE/IMAGE during a gameplay transition (e.g. the respawn frame
        # where the camera is momentarily at origin -> the camera heuristic reads SCENE, or a mid-load
        # frame) must NOT flash the diagnostic placeholder. Hold the last frame for a short grace period
        # after the last gameplay display; only fail loud if the scene PERSISTS (a real unrecovered scene).
        if (rt.cpu.instruction_count - last_gp_ic[0] < 90000 and boundary_capture[0] is not None):
            faithful_info[0] = "faithful: holding (transition)"
            return boundary_capture[0][0]
        # persistent unrecovered scene -> fail loud (no ASM VRAM fallback)
        if gap_seen[0] != cur_kind:
            gap_seen[0] = cur_kind
            print(f"[faithful] {FaithfulVisualGap(cur_kind)}", flush=True)
        faithful_info[0] = f"FAITHFUL GAP: {cur_kind.name} (see console)"
        return np.full((200, 320, 3), (48, 0, 32), dtype=np.uint8)

    def render_current():
        mem = bytes(rt.program.memory.data)
        mode = rt.dos.video_mode & 0x7F
        faithful_info[0] = ""
        if mode in (0, 1, 2, 3, 7):
            if faithful:
                # The faithful renderer is a clean RECREATION of the GAME's visual output; it never reads
                # the VM framebuffer. A DOS text mode is not game content, so it shows an explicit marker
                # (never the ASM text VRAM).
                faithful_info[0] = "faithful: DOS text mode (not game content)"
                rgb = np.full((200, 320, 3), (16, 16, 24), dtype=np.uint8)
            else:
                rgb = render_text_rgb(mem, rt.dos.video_mode & 0xFF, rt.dos.video_page)
        elif mode in (0x13, 0x19):
            if faithful:
                # FAITHFUL 13h: re-render the recovered image (identified at the 91C0 copy) from the
                # decoded asset + the live DAC palette — NEVER read the A000 framebuffer. An unidentified
                # image fails LOUD (gap), never falls back to VM VRAM.
                cur = current_13h_image[0]
                rgb = None
                if cur is not None:
                    name, has_logo = cur
                    try:
                        img = render_image_scene(name, args.game_root, with_logo=has_logo)
                        pal = np.array(rt.dos.vga_palette or [(0, 0, 0)] * 256, dtype=np.uint8)
                        rgb = pal[np.frombuffer(img, dtype=np.uint8).reshape(200, 320)]
                        faithful_info[0] = f"faithful[IMAGE:{name}]"
                    except Exception:
                        rgb = None
                if rgb is None:
                    if gap_seen[0] != "13h":
                        gap_seen[0] = "13h"
                        print("[faithful] mode-13h image not identified (no recovered leaf yet)", flush=True)
                    faithful_info[0] = "FAITHFUL GAP: 13h image (see console)"
                    rgb = np.full((200, 320, 3), (48, 0, 32), dtype=np.uint8)
            else:
                rgb = render_vga_rgb(mem, rt.dos.vga_palette)
        elif rt.program.memory.ega_planar:
            ds = rt.program.memory.ega_display_start
            if faithful:
                # Live FAITHFUL VISUAL path: the displayed image comes from the recovered visual
                # dispatcher (gameplay frame / iris transition), not ASM VRAM. Scenes whose leaf is
                # not recovered yet fall back to the VM frame inside _faithful_planar.
                rgb = _faithful_planar(mem, ds)
            else:
                # Interim: PRE2's intro/menu currently runs in 16-colour planar mode
                # 0Dh in the VM (the VGA mode-13h path is not yet taken).  Render it so
                # the screens are visible/navigable; colours come from the live DAC.
                rgb = render_planar_rgb(mem, ds, rt.dos.vga_palette, rt.program.memory.ega_pel_pan)
        else:
            screen.fill((0, 0, 0))
            pygame.display.flip()
            return
        last_rgb[0] = rgb
        _present_surface(pygame, np, screen, rgb)

    def replay_deliver(runtime, scancode: int) -> None:
        deliver_scancode(runtime, scancode, max_steps=args.input_irq_steps)

    try:
        while running and (args.steps is None or steps_done < args.steps):
            if replaying and playback.finished(frame):
                status = "demo replay complete"
                running = False
                break

            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F12:
                    out = _default_snapshot_dir(ROOT / "artifacts")
                    write_snapshot(rt, out, status="manual viewer snapshot", steps=steps_done)
                    print(f"snapshot: {out}")
                elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F10:
                    rgb = last_rgb[0]
                    if rgb is not None:
                        h, w = rgb.shape[0], rgb.shape[1]
                        surf = pygame.image.frombuffer(
                            np.ascontiguousarray(rgb).tobytes(), (w, h), "RGB")
                        out = ROOT / "artifacts" / f"shot_pre2_{datetime.now():%Y%m%d_%H%M%S}.png"
                        pygame.image.save(surf, str(out))
                        print(f"screenshot: {out}")
                elif replaying:
                    continue  # ignore host keys while a demo drives input
                elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F11:
                    if demo["rec"] is None:
                        start_recording(args.record_demo or "pre2")
                    else:
                        stop_recording()
                elif ev.type == pygame.KEYDOWN:
                    item = scan_map.get(ev.key)
                    if item is not None:
                        sc, _ = item
                        key_dispatcher.post_down(sc)
                        pending_dos.append((sc, getattr(ev, "unicode", "")))
                elif ev.type == pygame.KEYUP:
                    item = scan_map.get(ev.key)
                    if item is not None:
                        key_dispatcher.post_up(item[0])

            # Single canonical per-frame input delivery point (pre-step) so record
            # and replay see input at the identical VM position every frame.
            if replaying:
                playback.apply_to_runtime(frame, rt, deliver=replay_deliver)
            else:
                key_dispatcher.pump(allow_release=True)
                flush_dos_keys()

            # Live play self-paces on the wall clock via the emulated PIT/retrace;
            # demo record/replay use the deterministic emulated clock (same PIT/SB
            # servicing path, but a reproducible time source).
            realtime = not replaying and demo["rec"] is None
            if realtime != prev_realtime:
                # Re-anchor both clocks when the source switches (e.g. F11 starts a
                # recording) so the SB block timer and tick accumulator stay
                # continuous — otherwise the next block would stall or burst.
                now0 = perf_counter()
                vclock["base"] = now0 - rt.cpu.instruction_count / det_speed  # det_now()==now0
                tick_state["next"] = det_now() if not realtime else now0
                if sound_blaster is not None:
                    sound_blaster.clock = perf_counter if realtime else det_now
                    # Anchored cadence only on the wall clock; the det clock stays on the
                    # relative form so recordings remain byte-reproducible.
                    sound_blaster.anchor_cadence = realtime
                    sound_blaster.resync_clock(det_now() if not realtime else now0)
                sim_deadline = now0          # restart demo pacing from now
                last_render = 0.0            # force a render on the first demo frame
                prev_realtime = realtime
            rt.dos.time_source = perf_counter if realtime else det_now
            pic = rt.dos.pic
            try:
                if realtime:
                    # IRQs are raised on the wall clock and delivered at *batch
                    # boundaries* (not mid-instruction) so an ISR never interrupts a
                    # stateful EGA render sequence.
                    deadline = perf_counter() + present_period
                    frame_steps = 0
                    while running and perf_counter() < deadline:
                        if live_cpu_budget is None or frame_steps < live_cpu_budget:
                            _pump_and_step(rt, now=perf_counter(), pic=pic, sound_blaster=sound_blaster,
                                           timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                           tick_state=tick_state, n_steps=live_irq_batch)
                            steps_done += live_irq_batch
                            frame_steps += live_irq_batch
                        # else: budget spent — wait out the frame (audio still pumped below)
                        # Feed the audio device *continuously* (throttled), not just once
                        # per rendered frame below: the render can stretch a frame past
                        # the mixer channel's buffered depth, leaving its queue slot empty
                        # and underrunning.  Pumping every few ms keeps the slot filled.
                        nowp = perf_counter()
                        if nowp - last_audio >= 0.004:
                            if sb_audio is not None:
                                sb_audio.pump()
                            last_audio = nowp
                else:
                    chunk = args.chunk_steps if args.steps is None else min(args.chunk_steps, args.steps - steps_done)
                    _advance_demo_frame(rt, chunk_steps=chunk, sub_batch=realtime_batch,
                                        clock=det_now, pic=pic, sound_blaster=sound_blaster,
                                        timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                        tick_state=tick_state)
                    steps_done += chunk
            except ConsoleInputWouldBlock:
                status = "waiting for DOS key"
            except HaltExecution:
                status = "program halted"
                running = False
            except UnsupportedInstruction as exc:
                status = f"unsupported instruction: {exc}"
                running = False
            except Exception as exc:  # noqa: BLE001 - keep bring-up useful
                status = f"exception: {type(exc).__name__}: {exc}"
                import traceback as _tb
                _tb.print_exc()       # full traceback to stderr to pinpoint the failure
                running = False

            # Audio is drained every game-frame (cheap, and pcm_out must not pile up).
            if audio_poll is not None:     # detect song/music changes at a frame boundary
                audio_poll()
            if sb_audio is not None:
                sb_audio.pump()

            now = perf_counter()
            # Live play renders every frame (it is already wall-clock paced). The demo
            # path renders at most ~present_hz, and when it falls behind real time it
            # renders far less often (down to ~4 Hz) so the VM/audio get the wall-clock
            # time instead of the whole loop collapsing to the render rate.
            render_gap = present_period if (sim_deadline - now) > -present_period else 0.25
            do_render = realtime or (now - last_render) >= render_gap
            if do_render:
                render_current()
                caption_extra = audio_status.get("text", "")
                trace = getattr(rt, "_trace_stats", None)
                if trace is not None:
                    # Show only what is firing *now* (this window), not the growing cumulative
                    # list. The full per-hook totals are printed once at exit (_verify_summary).
                    caption_extra = ((caption_extra + " | ") if caption_extra else "") \
                        + f"hooks now: {trace.summary(group=_hook_group, top=6, since=trace_marker)}"
                    if now - last_trace >= 1.5:
                        last_trace = now
                        print(f"[hook-trace] now {trace.window_total(trace_marker)} fires | "
                              f"{trace.summary(group=_hook_group, since=trace_marker)}", flush=True)
                        trace_marker = trace.snapshot()
                pygame.display.set_caption(
                    f"PRE2 VM | {status} | frame={frame} steps={steps_done:,} | "
                    f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X} | mode={rt.dos.video_mode & 0xFF:02X}h"
                    + (f" | {caption_extra}" if caption_extra else "")
                    + (f" | {faithful_info[0]}" if faithful_info[0] else "")
                    + (" | REC" if demo["rec"] is not None else "")
                )
                last_render = now
            frame += 1
            if not realtime:
                # Pace game-frames to real time (each is present_period of game time):
                # sleep when ahead; if we fell far behind, resync so it never spirals.
                sim_deadline += present_period
                slack = sim_deadline - perf_counter()
                if slack > 0:
                    sleep(slack)
                elif slack < -0.5:
                    sim_deadline = perf_counter()
    finally:
        if not replaying:
            stop_recording()
        if sb_audio is not None:
            sb_audio.close()
        if getattr(rt, "_verify_summary", None) is not None:
            rt._verify_summary(" final:")
        pygame.quit()

    print(f"status: {status}")
    print(f"frames: {frame}  steps: {steps_done:,}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    print(f"video: mode={rt.dos.video_mode:02X}h text={rt.dos.text_mode_active} page={rt.dos.video_page}")
    if args.save_snapshot:
        out = _default_snapshot_dir(ROOT / "artifacts") if args.save_snapshot == "auto" else Path(args.save_snapshot)
        write_snapshot(rt, out, status=status, steps=steps_done)
        print(f"snapshot: {out}")
    return 0 if not status.startswith(("unsupported", "exception")) else 1


def _run_replay_headless(rt, args: argparse.Namespace, playback: InputDemoPlayback) -> int:
    """Replay a recorded demo with no UI: deterministic, fast, for testing.

    Mirrors the viewer's demo path exactly (same per-frame input point, fixed step
    budget, same deterministic emulated clock driving the PIT + Sound Blaster) minus
    presentation, so the resulting VM state matches what the viewer would reach.
    """
    from dos_re.cpu import HaltExecution, UnsupportedInstruction
    from dos_re.runtime import enable_sound_blaster

    steps_done = 0
    frame = 0
    status = "demo replay complete"

    realtime_batch = 2000
    # Enable the SB so the instruction stream matches a recording made with sound;
    # there is no audio sink here, so we drop the accumulated PCM each frame.
    det_speed = max(1, int(args.chunk_steps) * max(1, int(args.present_hz)))
    tick_state = {"next": 0.0}
    det_now = lambda: rt.cpu.instruction_count / det_speed  # noqa: E731 — per-instruction clock
    sound_blaster = None
    if getattr(args, "audio", "adlib") != "off":
        sound_blaster = enable_sound_blaster(rt)
        sound_blaster.clock = det_now
    rt.dos.time_source = det_now
    pic = rt.dos.pic

    def replay_deliver(runtime, scancode: int) -> None:
        deliver_scancode(runtime, scancode, max_steps=args.input_irq_steps)

    while (args.steps is None or steps_done < args.steps) and not playback.finished(frame):
        playback.apply_to_runtime(frame, rt, deliver=replay_deliver)
        chunk = args.chunk_steps if args.steps is None else min(args.chunk_steps, args.steps - steps_done)
        try:
            _advance_demo_frame(rt, chunk_steps=chunk, sub_batch=realtime_batch,
                                clock=det_now, pic=pic, sound_blaster=sound_blaster,
                                timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                tick_state=tick_state)
            steps_done += chunk
            if sound_blaster is not None and sound_blaster.pcm_out:
                sound_blaster.pcm_out.clear()
        except HaltExecution:
            status = "program halted"
            break
        except UnsupportedInstruction as exc:
            status = f"unsupported instruction: {exc}"
            break
        except Exception as exc:  # noqa: BLE001
            status = f"exception: {type(exc).__name__}: {exc}"
            break
        frame += 1

    if getattr(rt, "_verify_summary", None) is not None:
        rt._verify_summary(" final:")
    print(f"status: {status}")
    print(f"frames: {frame}  steps: {steps_done:,}  events_applied={playback.next_event_index}/{len(playback.events)}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    print(f"video: mode={rt.dos.video_mode:02X}h text={rt.dos.text_mode_active} page={rt.dos.video_page}")
    if args.save_snapshot:
        out = _default_snapshot_dir(ROOT / "artifacts") if args.save_snapshot == "auto" else Path(args.save_snapshot)
        write_snapshot(rt, out, status=status, steps=steps_done)
        print(f"snapshot: {out}")
    return 0 if not status.startswith(("unsupported", "exception")) else 1


def _make_replay_runtime(args: argparse.Namespace, playback: InputDemoPlayback):
    """Build a runtime from the demo's start snapshot, honouring recorded knobs."""
    meta = playback.manifest.get("metadata", {})
    # A demo must replay under the same step budget and bootstrap settings it was
    # recorded with, or the deterministic frame clock drifts.
    if "chunk_steps" in meta:
        args.chunk_steps = int(meta["chunk_steps"])
        # Demos recorded before the faithful multi-tick clock baked in a small
        # chunk (calibrated for the old 1-tick-per-frame timing).  Replayed under
        # the current clock the game gets far too few instructions/frame and runs
        # in slow motion — warn so it isn't mistaken for a performance problem.
        if args.chunk_steps < 8000:
            print(f"WARNING: this demo was recorded with chunk_steps={args.chunk_steps} "
                  "(old timing); under the current clock the game will run in slow "
                  "motion. Re-record the demo for correct speed.")
    # Clock knobs that feed the deterministic frame timer: replay under the recorded
    # values, falling back to the legacy defaults for demos recorded before they were
    # stored (present_hz=30, retrace_pulse=0.28) so old demos stay byte-reproducible.
    args.present_hz = int(meta.get("present_hz", 30))
    args.retrace_pulse = float(meta.get("retrace_pulse", 0.28))
    if "timer_irq" in meta:
        args.timer_irq = bool(meta["timer_irq"])
    if "input_irq_steps" in meta:
        args.input_irq_steps = int(meta["input_irq_steps"])
    fast_adlib = bool(meta.get("fast_adlib", getattr(args, "fast_adlib", False)))
    exe = Path(args.exe)
    game_root = Path(args.game_root)
    return load_pre2_snapshot(exe, playback.snapshot_path(), game_root=game_root, fast_adlib=fast_adlib)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prehistorik 2 DOS VM bootstrap/source-port runner (VGA + digital audio)")
    p.add_argument("--exe", default=str(ROOT / "assets" / "pre2.exe"), help="path to original PRE2.EXE")
    p.add_argument("--game-root", default=str(ROOT / "assets"), help="directory containing PRE2 assets")
    p.add_argument("--dos-args", default="", help="raw DOS command tail to pass to PRE2.EXE")
    p.add_argument("--snapshot", help="continue from an existing snapshot directory, or a DOSBox-X .sav save state")
    p.add_argument("--steps", type=int, default=None, help="max VM instructions to execute (default: unbounded in --view, 1,000,000 headless)")
    p.add_argument("--stop-at", type=parse_addr, help="stop before executing CS:IP, e.g. 1030:0100")
    p.add_argument("--trace-tail", type=int, default=40, help="number of recent trace lines to keep/print")
    p.add_argument("--save-snapshot", nargs="?", const="auto", help="save a VM snapshot; optional directory path")
    p.add_argument("--inventory", action="store_true", help="print PRE2 executable/asset inventory and exit")
    p.add_argument("--view", action="store_true", help="open the live pygame VGA/text viewer with digital audio")
    p.add_argument("--record-demo", metavar="NAME", help="(viewer) start recording an input demo immediately")
    p.add_argument("--play-demo", metavar="DIR", help="replay a recorded demo dir (headless unless --view)")
    p.add_argument("--demo-dir", default=str(ROOT / "artifacts"), help="directory to write recorded demos into")
    p.add_argument("--audio", default="adlib", choices=("adlib", "enhanced", "off"),
                   help="viewer digital audio: 'adlib' = faithful audio via the SB DMA path "
                        "(the recovered mixer's output); 'enhanced' = modern float mixer playing "
                        "the standard .TRK songs + SFX driven by the recovered audio commands; 'off'")
    p.add_argument("--scale", type=int, default=2, help="initial live viewer scale")
    p.add_argument("--faithful", action="store_true", help="(viewer) display GAMEPLAY frames from the recovered faithful renderer (render_frame on a clean framebuffer from explicit RendererState + assets) instead of the ASM-populated VRAM. The VM still runs as oracle/state-producer. Non-gameplay scenes (menu/intro/map) fall back to the VM frame (not yet recovered)")
    p.add_argument("--faithful-verify", action="store_true", help="(with --faithful) each gameplay frame, diff the recovered frame vs the VM's own page over the viewport and show the divergence in the title bar (surfaces any gameplay-state error; small residuals are the live moving-sprite blink-phase)")
    p.add_argument("--speed", type=int, default=450_000, help="emulated CPU steps/sec for the demo record/replay clock (steps-per-frame = speed/present-hz); the PIT/SB/retrace run at their true rates within that budget. Live --view ignores this and self-paces on the wall clock")
    p.add_argument("--chunk-steps", type=int, default=None, help="override VM steps per frame / demo clock (else derived from --speed and --present-hz)")
    p.add_argument("--present-hz", type=int, default=70, help="live presents per second (also paces the VM to real time); 70 matches the VGA refresh for a smooth present (demos replay at their recorded value)")
    p.add_argument("--retrace-pulse", type=float, default=0.06, help="(live) fraction of each refresh the VGA vertical-retrace status bit reads active. ~0.06 = realistic narrow VGA pulse that gates PRE2's mode-select scroll half-wait to one frame per 70Hz retrace (matches DOSBox); 0.28 = legacy wide window (~2x fast scroll). Demos replay at their recorded value")
    p.add_argument("--cpu-hz", type=int, default=0, help="(live) cap VM instructions/sec as an era-style ceiling (0 = unlimited, today's behavior). Caps ungated busy-loops + sets overall feel; tune by eye against DOSBox (our 'instruction' is not a real CPU cycle)")
    p.add_argument("--fast-adlib", action="store_true", help="mute/skip the hot PRE2 AdLib service thunk: reaches the game fastest, but mutes music")
    p.add_argument("--timer-irq", action=argparse.BooleanOptionalAction, default=True, help="deliver PRE2's INT 08h timer ISR each frame")
    p.add_argument("--input-irq-steps", type=int, default=2_000_000, help="maximum VM steps for one keyboard/timer interrupt")
    p.add_argument("--no-replacements", action="store_true", help="run the pure VM oracle with NO recovered/hybrid hooks (their fixed code/data offsets are bound to one build's layout; use this on a build they weren't derived against)")
    p.add_argument("--verify-hooks", action="store_true", help="run the original ASM as the oracle and diff each recovered-native result against it; prints divergences immediately plus a compact periodic per-hook summary")
    p.add_argument("--verify-verbose", action="store_true", help="(with --verify-hooks) print a line for every OK result, not just divergences + the periodic summary")
    p.add_argument("--full-verify", action="store_true", help="foolproof variant of --verify-hooks: diff the WHOLE machine state (all memory + return cs:ip:sp) after each recovered routine vs the ASM, so nothing can leak outside a hand-picked contract. ~10x slower; for offline snapshot/demo audits, not live play")
    p.add_argument("--trace-hooks", action="store_true", help="run the LIVE hybrid runtime (hooks replacing ASM, NOT the oracle) and show which recovered hooks fire — a live coverage view in the title bar + a periodic/final per-hook tally. Hooks absent = that screen is still pure ASM")
    args = p.parse_args(argv)
    # VM steps per frame: explicit override, else derived so that
    # chunk * present_hz == --speed steps/sec (the real-time tempo throttle).
    # A demo replay overrides this from the manifest in _make_replay_runtime.
    args.chunk_steps = args.chunk_steps or max(1, args.speed // max(1, args.present_hz))

    exe = Path(args.exe)
    game_root = Path(args.game_root)

    if args.inventory:
        inv = inventory_assets(game_root)
        desc = describe_exe(exe)
        print("Prehistorik 2 inventory")
        print(f"  exe: {inv.exe} ({inv.exe.stat().st_size:,} bytes)")
        print(f"  MZ entry: {desc['entry_cs']:04X}:{desc['entry_ip']:04X}")
        print(f"  MZ stack: {desc['initial_ss']:04X}:{desc['initial_sp']:04X}")
        print(f"  relocations: {desc['relocations']}  overlay: {desc['overlay_size']} bytes")
        print(f"  SQZ assets: {len(inv.sqz_files)}")
        print(f"  TRK music: {len(inv.trk_files)}")
        print(f"  docs/config files: {len(inv.docs)}")
        return 0

    if args.play_demo:
        playback = InputDemoPlayback.load(args.play_demo)
        rt = _make_replay_runtime(args, playback)
        _install_verification_hooks(rt, args)
        _install_hook_trace(rt, args)
        if args.view:
            return _run_view(rt, args, playback=playback)
        return _run_replay_headless(rt, args, playback)

    rt = _make_runtime(args)
    _install_verification_hooks(rt, args)
    _install_hook_trace(rt, args)
    if args.view:
        return _run_view(rt, args)

    status, steps, trace_tail = run_until(
        rt,
        max_steps=args.steps if args.steps is not None else 1_000_000,
        stop_at=args.stop_at,
        trace_tail=args.trace_tail,
    )

    if getattr(rt, "_verify_summary", None) is not None:
        rt._verify_summary(" final:")
    print(f"status: {status}")
    print(f"steps: {steps:,}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    print(f"video: mode={rt.dos.video_mode:02X}h text={rt.dos.text_mode_active} page={rt.dos.video_page}")
    if rt.dos.files:
        print("open files:")
        for handle, fh in sorted(rt.dos.files.items()):
            print(f"  {handle}: {fh.path.name} pos={fh.pos} size={len(fh.data)}")
    stdout = "".join(rt.dos.stdout).strip()
    if stdout:
        print("stdout tail:")
        print(stdout[-2000:])
    if trace_tail:
        print("trace tail:")
        for line in trace_tail:
            print(line)

    if args.save_snapshot:
        out = _default_snapshot_dir(ROOT / "artifacts") if args.save_snapshot == "auto" else Path(args.save_snapshot)
        write_snapshot(rt, out, status=status, steps=steps, trace_tail=trace_tail)
        print(f"snapshot: {out}")

    # A max-step stop is useful during RE, so it is not treated as failure.
    return 0 if not status.startswith("unsupported") and not status.startswith("exception") else 1


if __name__ == "__main__":
    raise SystemExit(main())
