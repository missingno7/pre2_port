"""Run Prehistorik 2 inside the DOS_RE VM.

This is intentionally a bootstrap/source-port runner, not a finished game
frontend.  It starts the original packed PRE2.EXE, accelerates the LZEXE stub,
and stops/snapshots at deterministic VM boundaries so we can begin lifting the
real game code from evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.keyboard import KeyDispatcher
from dos_re.snapshot import parse_addr, run_until, write_snapshot
from pre2.analysis import describe_exe, inventory_assets
from pre2.launch import build_command_tail
from pre2.runtime import create_pre2_runtime, load_pre2_snapshot


def _default_snapshot_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / f"snapshot_pre2_{stamp}"




def _make_runtime(args: argparse.Namespace):
    exe = Path(args.exe)
    game_root = Path(args.game_root)
    command_tail = build_command_tail(args.dos_args)
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


def _run_view(rt, args: argparse.Namespace) -> int:
    """Very small live VGA/text viewer for PRE2 bring-up.

    This intentionally avoids target hooks/frame boundaries.  It runs a bounded
    chunk of original VM instructions, then presents whatever the emulated video
    hardware currently exposes.  That is enough to make VGA/text startup visible
    while the real source-port boundaries are still being discovered.
    """
    import pygame
    import numpy as np
    from sdl_view import render_text_rgb, render_vga_rgb, render_ega_rgb, render_tandy_rgb, render_cga_rgb
    from dos_re.cpu import HaltExecution, UnsupportedInstruction
    from dos_re.dos import ConsoleInputWouldBlock

    pygame.init()
    scale = max(1, int(args.scale))
    screen = pygame.display.set_mode((640 * scale, 400 * scale), pygame.RESIZABLE)
    pygame.display.set_caption("PRE2 DOS_RE VM - starting")
    scan_map = _pygame_scan_map(pygame)
    clock = pygame.time.Clock()
    steps_done = 0
    running = True
    status = "running"
    rt.cpu.trace_enabled = False
    # Let live input block instead of synthesizing Esc in true blocking DOS reads.
    rt.dos.console_input_fallback = None

    def deliver_input(scancode: int) -> None:
        nonlocal status
        try:
            deliver_scancode(rt, scancode, max_steps=args.input_irq_steps)
        except Exception as exc:  # noqa: BLE001 - keep viewer alive during RE
            status = f"keyboard interrupt failed: {type(exc).__name__}: {exc}"

    key_dispatcher = KeyDispatcher(deliver_input)

    def render_current():
        mem = bytes(rt.program.memory.data)
        mode = rt.dos.video_mode & 0x7F
        if mode in (0, 1, 2, 3, 7):
            rgb = render_text_rgb(mem, mode, rt.dos.video_page)
        elif mode in (0x13, 0x19):
            rgb = render_vga_rgb(mem, rt.dos.vga_palette)
        elif rt.program.memory.ega_planar:
            rgb = render_ega_rgb(mem, rt.program.memory.ega_display_start)
        elif args.video == "tandy":
            rgb = render_tandy_rgb(mem)
        else:
            rgb = render_cga_rgb(mem, args.palette)
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

    try:
        while running and steps_done < args.steps:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_F12:
                        out = _default_snapshot_dir(ROOT / "artifacts")
                        write_snapshot(rt, out, status="manual viewer snapshot", steps=steps_done)
                        print(f"snapshot: {out}")
                    else:
                        item = scan_map.get(ev.key)
                        if item is not None:
                            sc, ascii_code = item
                            if ascii_code:
                                rt.dos.key_queue.append(((sc & 0xFF) << 8) | (ascii_code & 0xFF))
                            key_dispatcher.post_down(sc)
                elif ev.type == pygame.KEYUP:
                    item = scan_map.get(ev.key)
                    if item is not None:
                        sc, _ = item
                        key_dispatcher.post_up(sc)

            # Pump once before the original game advances so quick UI taps are
            # held through at least one emulated polling boundary.
            key_dispatcher.pump(allow_release=False)

            chunk = min(args.chunk_steps, args.steps - steps_done)
            try:
                for _ in range(chunk):
                    rt.cpu.step()
                steps_done += chunk
                if args.timer_irq:
                    deliver_interrupt(rt, 0x08, max_steps=args.input_irq_steps)
                    key_dispatcher.pump(allow_release=True)
                else:
                    key_dispatcher.pump_events()
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

            render_current()
            pygame.display.set_caption(
                f"PRE2 DOS_RE VM | {status} | steps={steps_done:,} | "
                f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X} | mode={rt.dos.video_mode & 0xFF:02X}h"
            )
            clock.tick(max(1, int(args.present_hz)))
    finally:
        pygame.quit()

    print(f"status: {status}")
    print(f"steps: {steps_done:,}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    print(f"video: mode={rt.dos.video_mode:02X}h text={rt.dos.text_mode_active} page={rt.dos.video_page}")
    return 0 if not status.startswith(("unsupported", "exception")) else 1

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prehistorik 2 DOS VM bootstrap/source-port runner")
    p.add_argument("--exe", default=str(ROOT / "assets" / "pre2.exe"), help="path to original PRE2.EXE")
    p.add_argument("--game-root", default=str(ROOT / "assets"), help="directory containing PRE2 assets")
    p.add_argument("--dos-args", default="", help="raw DOS command tail to pass to PRE2.EXE")
    p.add_argument("--snapshot", help="continue from an existing snapshot directory")
    p.add_argument("--steps", type=int, default=1_000_000, help="maximum VM instructions/hooks to execute")
    p.add_argument("--stop-at", type=parse_addr, help="stop before executing CS:IP, e.g. 1996:0100")
    p.add_argument("--trace-tail", type=int, default=40, help="number of recent trace lines to keep/print")
    p.add_argument("--save-snapshot", nargs="?", const="auto", help="save a VM snapshot; optional directory path")
    p.add_argument("--inventory", action="store_true", help="print PRE2 executable/asset inventory and exit")
    p.add_argument("--view", action="store_true", help="open a simple live pygame VGA/text viewer")
    p.add_argument("--video", default="vga", choices=("vga", "ega", "tandy", "cga"), help="fallback decoder before PRE2 switches to a known BIOS mode")
    p.add_argument("--palette", default="1h", help="CGA fallback palette")
    p.add_argument("--scale", type=int, default=2, help="initial live viewer scale")
    p.add_argument("--chunk-steps", type=int, default=8000, help="VM steps to run between live presents")
    p.add_argument("--present-hz", type=int, default=30, help="maximum live presents per second")
    p.add_argument("--fast-adlib", action="store_true", help="mute/skip the hot PRE2 AdLib service thunk during bring-up")
    p.add_argument("--timer-irq", action=argparse.BooleanOptionalAction, default=True, help="deliver PRE2's INT 08h timer ISR between live frames")
    p.add_argument("--input-irq-steps", type=int, default=2_000_000, help="maximum VM steps for one keyboard/timer interrupt")
    args = p.parse_args(argv)

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

    rt = _make_runtime(args)
    if args.view:
        return _run_view(rt, args)

    status, steps, trace_tail = run_until(
        rt,
        max_steps=args.steps,
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
