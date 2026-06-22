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

    def _summary(tag: str = "") -> None:
        grouped: dict[str, list[int]] = {}
        for name, c in counts.items():
            g = grouped.setdefault(_group(name), [0, 0])
            g[0] += c[0]
            g[1] += c[1]
        parts = " ".join(
            f"{n}={c[0]}" + (f"✗{c[1]}" if c[1] else "")
            for n, c in sorted(grouped.items(), key=lambda kv: -sum(kv[1]))
        )
        total = sum(c[0] + c[1] for c in counts.values())
        flag = "OK" if divergences[0] == 0 else f"{divergences[0]} DIVERGENCE(S)"
        print(f"[verify-hooks]{tag} {total} checks, {flag} | {parts}", flush=True)

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
                _summary()

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
    from sdl_view import SoundBlasterAudio, render_planar_rgb, render_text_rgb, render_vga_rgb
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
        # The SB is enabled either way: the game detects a digital device and runs its
        # song-loader / play-SFX commands (which we observe), and it keeps the original
        # audio timing identical to the faithful path.
        sound_blaster = enable_sound_blaster(rt)
        if audio_mode == "enhanced":
            # Modern path: observe the recovered audio *commands* and play the standard
            # .TRK songs + SFX through the enhanced float mixer (the SB PCM is ignored).
            from sdl_view import EnhancedAudio
            from pre2.audio.enhanced_backend import EnhancedBackend
            from pre2.bridge.audio_commands import install_command_observers
            # Let the ORIGINAL ASM run the game's audio here, by removing the recovered
            # tracker/mixer checkpoints.  We don't use their output (the enhanced backend is
            # the audio; we only consume SB block production as the tick clock + need the
            # game's state to advance).  Crucially, the recovered mix_channel has a known
            # state divergence that, run live, corrupts the game's channel state until a
            # loop region degenerates and the mixer spins forever (freeze).  A fresh cold
            # boot on the pure ASM audio runs clean for tens of millions of instructions.
            # Kept under --verify-hooks (verification uses the ASM result, so no corruption).
            if not getattr(args, "verify_hooks", False):
                for _addr in ((0x1030, 0x227C), (0x1030, 0x218F)):   # tracker, mixer
                    rt.cpu.replacement_hooks.pop(_addr, None)
                    rt.cpu.hook_names.pop(_addr, None)
            # Fully detached from the DOS audio machine: the enhanced mixer free-runs the
            # song at its own musical tempo and is driven ONLY by semantic events
            # (StartSong / PlaySfx / SetMusicEnabled) from the recovered command layer --
            # no SB block counting, no DMA/IRQ, no original mixer PCM.
            _enh = EnhancedBackend(free_run=True)
            # EnhancedAudio owns the backend + a dedicated audio thread; events from the VM
            # are injected through its thread-safe handle (audio runs on its own clock).
            sb_audio = EnhancedAudio(pygame, _enh, sound_blaster, audio_status)
            audio_poll = install_command_observers(rt.cpu, sb_audio.handle, args.game_root)
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

    def render_current():
        mem = bytes(rt.program.memory.data)
        mode = rt.dos.video_mode & 0x7F
        if mode in (0, 1, 2, 3, 7):
            rgb = render_text_rgb(mem, rt.dos.video_mode & 0xFF, rt.dos.video_page)
        elif mode in (0x13, 0x19):
            rgb = render_vga_rgb(mem, rt.dos.vga_palette)
        elif rt.program.memory.ega_planar:
            # Interim: PRE2's intro/menu currently runs in 16-colour planar mode
            # 0Dh in the VM (the VGA mode-13h path is not yet taken).  Render it so
            # the screens are visible/navigable; colours come from the live DAC.
            rgb = render_planar_rgb(mem, rt.program.memory.ega_display_start, rt.dos.vga_palette)
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
                    while running and perf_counter() < deadline:
                        _pump_and_step(rt, now=perf_counter(), pic=pic, sound_blaster=sound_blaster,
                                       timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                       tick_state=tick_state, n_steps=live_irq_batch)
                        steps_done += live_irq_batch
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
                pygame.display.set_caption(
                    f"PRE2 VM | {status} | frame={frame} steps={steps_done:,} | "
                    f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X} | mode={rt.dos.video_mode & 0xFF:02X}h"
                    + (f" | {caption_extra}" if caption_extra else "")
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
    p.add_argument("--speed", type=int, default=450_000, help="emulated CPU steps/sec for the demo record/replay clock (steps-per-frame = speed/present-hz); the PIT/SB/retrace run at their true rates within that budget. Live --view ignores this and self-paces on the wall clock")
    p.add_argument("--chunk-steps", type=int, default=None, help="override VM steps per frame / demo clock (else derived from --speed and --present-hz)")
    p.add_argument("--present-hz", type=int, default=30, help="live presents per second (also paces the VM to real time)")
    p.add_argument("--fast-adlib", action="store_true", help="mute/skip the hot PRE2 AdLib service thunk: reaches the game fastest, but mutes music")
    p.add_argument("--timer-irq", action=argparse.BooleanOptionalAction, default=True, help="deliver PRE2's INT 08h timer ISR each frame")
    p.add_argument("--input-irq-steps", type=int, default=2_000_000, help="maximum VM steps for one keyboard/timer interrupt")
    p.add_argument("--no-replacements", action="store_true", help="run the pure VM oracle with NO recovered/hybrid hooks (their fixed code/data offsets are bound to one build's layout; use this on a build they weren't derived against)")
    p.add_argument("--verify-hooks", action="store_true", help="run the original ASM as the oracle and diff each recovered-native result against it; prints divergences immediately plus a compact periodic per-hook summary")
    p.add_argument("--verify-verbose", action="store_true", help="(with --verify-hooks) print a line for every OK result, not just divergences + the periodic summary")
    p.add_argument("--full-verify", action="store_true", help="foolproof variant of --verify-hooks: diff the WHOLE machine state (all memory + return cs:ip:sp) after each recovered routine vs the ASM, so nothing can leak outside a hand-picked contract. ~10x slower; for offline snapshot/demo audits, not live play")
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
        if args.view:
            return _run_view(rt, args, playback=playback)
        return _run_replay_headless(rt, args, playback)

    rt = _make_runtime(args)
    _install_verification_hooks(rt, args)
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
