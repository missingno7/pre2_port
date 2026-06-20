"""Run Prehistorik 2 inside the DOS_RE VM.

This is intentionally a bootstrap/source-port runner, not a finished game
frontend.  It starts the original packed PRE2.EXE in pure original ASM (only the
LZEXE bootstrap accelerator and optional helpers are installed, never gameplay
hooks), and stops/snapshots/records at deterministic VM boundaries so we can begin
lifting the real game code from evidence.

PRE2 uses BIOS text, linear VGA, and a VGA/EGA-compatible 320x200 16-colour
planar graphics path.  The viewer renders those VM-visible video states and
drives the vendored Nuked-OPL3 backend from the original AdLib register stream.

Three ways to use it:
  * ``--view``                live VGA/text viewer + OPL3 audio; F11 records a
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

from dos_re.input_demo import InputDemoPlayback, InputDemoRecorder, bios_key_value_from_scancode
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.keyboard import KeyDispatcher
from dos_re.snapshot import parse_addr, run_until, write_snapshot
from pre2.analysis import describe_exe, inventory_assets
from pre2.launch import build_command_tail
from pre2.runtime import create_pre2_runtime, load_pre2_snapshot


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
    if not getattr(args, "verify_hooks", False):
        return
    from pre2.replacements import enable_pre2_hook_verification

    def _on_result(name: str, ok: bool, detail) -> None:
        if ok:
            print(f"[verify-hooks] OK          {name}", flush=True)
        else:
            print(f"[verify-hooks] DIVERGENCE  {name}: {detail}", flush=True)

    enable_pre2_hook_verification(rt, on_result=_on_result)
    print("[verify-hooks] lockstep oracle active: native hooks diffed vs original ASM", flush=True)


def _make_runtime(args: argparse.Namespace, *, fast_adlib: bool | None = None):
    exe = Path(args.exe)
    game_root = Path(args.game_root)
    command_tail = build_command_tail(args.dos_args)
    if fast_adlib is None:
        fast_adlib = bool(getattr(args, "fast_adlib", False))
    if args.snapshot:
        return load_pre2_snapshot(exe, args.snapshot, game_root=game_root, fast_adlib=fast_adlib)
    return create_pre2_runtime(exe, game_root=game_root, command_tail=command_tail, fast_adlib=fast_adlib)


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


def _run_view(rt, args: argparse.Namespace, *, playback: InputDemoPlayback | None = None) -> int:
    """Live VGA/text viewer for PRE2 bring-up, with OPL3 audio and demo record/replay.

    This intentionally avoids gameplay hooks/frame boundaries.  It advances a
    fixed ``chunk_steps`` of original VM instructions per displayed frame, then
    presents whatever the emulated VGA/text hardware exposes.  The fixed step
    budget per frame is what makes the frame counter a deterministic demo clock:
    a recorded demo replays identically as long as ``chunk_steps``/``timer_irq``/
    ``fast_adlib`` match (they are stored in the demo manifest).
    """
    import pygame
    import numpy as np
    from time import perf_counter
    from sdl_view import NukedAdlibAudio, render_planar_rgb, render_text_rgb, render_vga_rgb
    from dos_re.cpu import HaltExecution, UnsupportedInstruction, IF
    from dos_re.dos import ConsoleInputWouldBlock

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
    next_tick = perf_counter()
    realtime_batch = 2000  # VM steps between wall-clock checks (keeps ticks on time)
    status = "replaying" if replaying else "running"
    rt.cpu.trace_enabled = False
    rt.dos.console_input_fallback = None

    # Sound-card (OPL3/AdLib) audio: the VM runs the original AdLib driver and
    # forwards YM3812 register writes; this turns that stream into PCM.
    audio_status: dict[str, str] = {}
    adlib = None
    if getattr(args, "audio", "adlib") == "adlib":
        adlib = NukedAdlibAudio(pygame, audio_status, enabled=True)
        rt.dos.set_adlib_callback(lambda reg, value: adlib.write(reg, value), emit_current=True)

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
            # record/replay keep the deterministic fixed-chunk clock.
            realtime = not replaying and demo["rec"] is None
            rt.dos.time_source = perf_counter if realtime else None
            try:
                if realtime:
                    deadline = perf_counter() + present_period
                    while running and perf_counter() < deadline:
                        now = perf_counter()
                        tick_period = 1.0 / max(1.0, rt.dos.pit_channel0_hz())
                        if args.timer_irq and now >= next_tick and rt.cpu.get_flag(IF):
                            deliver_interrupt(rt, 0x08, max_steps=args.input_irq_steps)
                            next_tick += tick_period
                            if next_tick < now:  # fell behind: resync, no tick burst
                                next_tick = now + tick_period
                        else:
                            for _ in range(realtime_batch):
                                rt.cpu.step()
                            steps_done += realtime_batch
                else:
                    chunk = args.chunk_steps if args.steps is None else min(args.chunk_steps, args.steps - steps_done)
                    for _ in range(chunk):
                        rt.cpu.step()
                    steps_done += chunk
                    if args.timer_irq:
                        deliver_interrupt(rt, 0x08, max_steps=args.input_irq_steps)
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
                running = False

            if adlib is not None:
                adlib.pump()
            render_current()
            caption_extra = audio_status.get("text", "")
            pygame.display.set_caption(
                f"PRE2 VM | {status} | frame={frame} steps={steps_done:,} | "
                f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X} | mode={rt.dos.video_mode & 0xFF:02X}h"
                + (f" | {caption_extra}" if caption_extra else "")
                + (" | REC" if demo["rec"] is not None else "")
            )
            frame += 1
            if not realtime:
                clock.tick(max(1, int(args.present_hz)))
    finally:
        if not replaying:
            stop_recording()
        if adlib is not None:
            adlib.close()
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

    Mirrors the viewer loop exactly (same per-frame input point, fixed step
    budget, optional timer IRQ) minus presentation, so the resulting VM state
    matches what the viewer would reach.
    """
    from dos_re.cpu import HaltExecution, UnsupportedInstruction

    steps_done = 0
    frame = 0
    status = "demo replay complete"

    def replay_deliver(runtime, scancode: int) -> None:
        deliver_scancode(runtime, scancode, max_steps=args.input_irq_steps)

    while (args.steps is None or steps_done < args.steps) and not playback.finished(frame):
        playback.apply_to_runtime(frame, rt, deliver=replay_deliver)
        chunk = args.chunk_steps if args.steps is None else min(args.chunk_steps, args.steps - steps_done)
        try:
            for _ in range(chunk):
                rt.cpu.step()
            steps_done += chunk
            if args.timer_irq:
                deliver_interrupt(rt, 0x08, max_steps=args.input_irq_steps)
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
    if "timer_irq" in meta:
        args.timer_irq = bool(meta["timer_irq"])
    if "input_irq_steps" in meta:
        args.input_irq_steps = int(meta["input_irq_steps"])
    fast_adlib = bool(meta.get("fast_adlib", getattr(args, "fast_adlib", False)))
    exe = Path(args.exe)
    game_root = Path(args.game_root)
    return load_pre2_snapshot(exe, playback.snapshot_path(), game_root=game_root, fast_adlib=fast_adlib)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prehistorik 2 DOS VM bootstrap/source-port runner (VGA + OPL3)")
    p.add_argument("--exe", default=str(ROOT / "assets" / "pre2.exe"), help="path to original PRE2.EXE")
    p.add_argument("--game-root", default=str(ROOT / "assets"), help="directory containing PRE2 assets")
    p.add_argument("--dos-args", default="", help="raw DOS command tail to pass to PRE2.EXE")
    p.add_argument("--snapshot", help="continue from an existing snapshot directory")
    p.add_argument("--steps", type=int, default=None, help="max VM instructions to execute (default: unbounded in --view, 1,000,000 headless)")
    p.add_argument("--stop-at", type=parse_addr, help="stop before executing CS:IP, e.g. 1996:0100")
    p.add_argument("--trace-tail", type=int, default=40, help="number of recent trace lines to keep/print")
    p.add_argument("--save-snapshot", nargs="?", const="auto", help="save a VM snapshot; optional directory path")
    p.add_argument("--inventory", action="store_true", help="print PRE2 executable/asset inventory and exit")
    p.add_argument("--view", action="store_true", help="open the live pygame VGA/text viewer with OPL3 audio")
    p.add_argument("--record-demo", metavar="NAME", help="(viewer) start recording an input demo immediately")
    p.add_argument("--play-demo", metavar="DIR", help="replay a recorded demo dir (headless unless --view)")
    p.add_argument("--demo-dir", default=str(ROOT / "artifacts"), help="directory to write recorded demos into")
    p.add_argument("--audio", default="adlib", choices=("adlib", "off"), help="viewer sound-card (OPL3) audio")
    p.add_argument("--scale", type=int, default=2, help="initial live viewer scale")
    p.add_argument("--speed", type=int, default=120_000, help="VM steps/sec for record/replay's deterministic clock; live --view ignores this and self-paces on the emulated PIT/retrace at native speed")
    p.add_argument("--chunk-steps", type=int, default=None, help="override VM steps per frame / demo clock (else derived from --speed and --present-hz)")
    p.add_argument("--present-hz", type=int, default=30, help="live presents per second (also paces the VM to real time)")
    p.add_argument("--fast-adlib", action="store_true", help="mute/skip the hot PRE2 AdLib service thunk: reaches the game fastest, but mutes music")
    p.add_argument("--timer-irq", action=argparse.BooleanOptionalAction, default=True, help="deliver PRE2's INT 08h timer ISR each frame")
    p.add_argument("--input-irq-steps", type=int, default=2_000_000, help="maximum VM steps for one keyboard/timer interrupt")
    p.add_argument("--verify-hooks", action="store_true", help="install recovered-native verification checkpoints (e.g. SQZ decode): the original ASM still runs and stays the oracle; each result is compared to native and printed OK/DIVERGENCE")
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
