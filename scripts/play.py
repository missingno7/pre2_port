"""Run Prehistorik 2 inside the DOS_RE VM — the hybrid recovered-source runtime.

A source-port runner, not a finished game frontend. **By default this is the HYBRID
runtime:** the original PRE2.EXE executes in the VM as the behavioural oracle, but
recovered native replacements run IN PLACE OF the ASM at every grounded hook (asset
decode, gameplay frame, sprite/object draw, scroll, audio, iris, scene drawers, …;
see docs/pre2/recovered_islands.md). The original ASM runs ONLY in oracle/verify modes.
Unrecovered behaviour fails LOUD (`Pre2HybridGap`) — never a silent fall-through to ASM.

Two independent axes — EXECUTION MODE and VIDEO BACKEND.

Execution mode (no silent fallbacks):
  * ``--view`` (default = HYBRID)  hybrid runtime: recovered native replacements run in place of
                                   the ASM. Live VGA/text viewer + digital audio.
  * ``--no-replacements``          ORACLE mode: pure original ASM, no recovered hooks.
  * ``--verify-hooks``             VERIFY mode: the ASM oracle runs and each recovered replacement is
                                   diffed against it at its contract boundary (``--full-verify`` =
                                   whole-machine-state diff).
  * ``--trace-hooks``              hybrid runtime + a live tally of which recovered hooks fire.

Video backend (how frames are DISPLAYED; independent of the execution mode):
  * ``--video vm`` (default)       the emulated VM/VGA framebuffer.
  * ``--video faithful``           the recovered FaithfulVisual backend (render_frame / render_visual +
                                   scene leaves from explicit state + assets); NEVER reads the VM
                                   framebuffer, fails LOUD on an unrecovered scene.
  * ``--video enhanced``           modern presentation layer on top of the faithful backend (projects the
                                   faithful frame; never the VM framebuffer); currently a passthrough baseline.

PRE2 uses BIOS text, linear VGA, and a 320x200 16-colour planar path; the viewer renders those
and plays the digital audio (MOD music + PCM SFX) via the emulated Sound Blaster DMA path (PRE2
GOG is digital-only, never OPL3/AdLib). F11 records an input demo, F12 saves a snapshot;
``--play-demo DIR`` replays a recorded demo (headless + deterministic, add ``--view`` to watch).
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_FRAME_HASH = bool(os.environ.get("PRE2_FRAME_HASH"))  # extraction-equivalence harness (deterministic replay)

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


def _advance_frame_deterministic(rt, args, *, chunk_steps, sub_batch, clock, pic, sound_blaster,
                                 timer_irq, input_irq_steps, tick_state, det_speed, base=0.0):
    """Advance one deterministic frame, fast-forwarding the recovered VGA retrace-wait timing primitive when
    the hybrid runtime is active (the default). This is the single decision point shared by every
    deterministic stepping path (headless replay, in-view demo replay, verify/oracle).

    The fast path (``pre2.bridge.timing_fastforward.advance_frame_fast``) is a recovered timing hook: it
    collapses the long runs of identical 9900/990D/44CD retrace polls in closed form but is BYTE-EQUIVALENT
    to the interpreted ``_advance_demo_frame`` on the deterministic clock (it mirrors the same sub_batch IRQ
    cadence and reproduces every register/flag/memory/port side effect). It is disabled — falling back to the
    pure interpreted ASM loops — under ``--no-replacements`` (pure oracle) or ``--no-fast-retrace-waits`` (so
    the original ASM timing path stays available for comparison)."""
    use_fast = getattr(args, "fast_retrace_waits", True) and not getattr(args, "no_replacements", False)
    if use_fast:
        from pre2.bridge.timing_fastforward import advance_frame_fast
        advance_frame_fast(rt, chunk_steps=chunk_steps, sub_batch=sub_batch, clock=clock, pic=pic,
                           sound_blaster=sound_blaster, timer_irq=timer_irq, input_irq_steps=input_irq_steps,
                           tick_state=tick_state, det_speed=det_speed,
                           active_fraction=rt.dos.vga_retrace_active_fraction, base=base)
    else:
        _advance_demo_frame(rt, chunk_steps=chunk_steps, sub_batch=sub_batch, clock=clock, pic=pic,
                            sound_blaster=sound_blaster, timer_irq=timer_irq,
                            input_irq_steps=input_irq_steps, tick_state=tick_state)


# --- Live --view scheduler-friendly retrace waits ------------------------------------------------------
# Live mode is wall-clock paced (time_source = perf_counter), so the deterministic instruction-count
# fast-forward does NOT apply — fast-forwarding would advance game time early. Instead, while the VM is parked
# in a classified retrace wait (1030:9900/990D/44CD), we YIELD the core (sleep) during the *safe interior* of
# the current retrace phase and busy-poll only the last ~1.5 ms before any phase edge, so the VM's own poll
# still exits at the same wall-clock instant. Same pacing / game timing / exit condition; just less CPU burn.
_LIVE_CS = 0x1030
_LIVE_POLL_BATCH = 32          # instructions per re-poll while parked (a few poll iterations + any due ISR)
_LIVE_PARK_MARGIN = 0.0015     # stop sleeping this long before a retrace edge (covers OS sleep overshoot)
_LIVE_PARK_MIN_SLICE = 0.0004  # don't bother sleeping for less than this (busy-poll instead)
_LIVE_PARK_MAX_SLICE = 0.004   # re-check at least this often (also the audio-pump cadence)

# The 1030:1C6F PIT-tick delay loop body (disasm-confirmed): `mov ax,[0x27ee]; sub ax,cs:[0x1d67];
# jns; (neg ax); cmp ax,3; jb 1C6F` — busy-waits until the timer counter [0x27ee] (advanced ONLY by the
# INT 08 timer ISR) has moved >=3 ticks. In live mode we park it as a WALL-CLOCK wait: sleep until the next
# PIT tick is due (tick_state["next"]) and let the normal IRQ pump advance [0x27ee]; never mutate it here.
_LIVE_PIT_NODES = frozenset((0x1C6F, 0x1C72, 0x1C77, 0x1C79, 0x1C7B, 0x1C7E))
_LIVE_PIT_MARGIN = 0.0008      # wake ~this long before the tick is due (the next pump delivers it on time)


def _time_to_next_retrace_edge(now: float, active_fraction: float, refresh_hz: float = 70.0) -> float:
    """Seconds until the emulated VGA retrace bit next changes state, under the live wall clock — matching
    ``dos._vga_status`` (phase = (now*refresh_hz) % 1; SET while phase >= 1-active_fraction). The bit toggles
    at phase ``1-active_fraction`` (CLEAR->SET) and at the period wrap ``1.0`` (SET->CLEAR)."""
    phase = (now * refresh_hz) % 1.0
    thr = 1.0 - active_fraction
    next_edge_phase = thr if phase < thr else 1.0
    return (next_edge_phase - phase) / refresh_hz


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
    from pre2.bridge.gameover_scene import build_gameover_scene, load_gameover_asset, _object_overlay
    from pre2.bridge.render_state import read_renderer_state, retarget_page
    from pre2.bridge.tally_scene import build_tally_scene
    from pre2.bridge.oldies_scene import build_oldies_scene
    from pre2.bridge.tally_panel import read_tally_panel
    from pre2.bridge.image_scene import identify_image, render_image_scene
    from pre2.bridge.scene_state import derive_scene_kind
    from pre2.bridge import present as _present_bridge
    from pre2.bridge import text as _text_bridge
    from pre2.recovered.gameover_background import render_gameover_background
    from pre2.recovered.carte import build_carte_page
    from pre2.recovered.menu_scene import MenuScenePage
    from pre2.recovered.scene_compositor import RecoveredBackground
    from pre2.recovered.faithful_visual import FaithfulVisualGap, SceneKind
    from pre2.bridge.faithful_session import FaithfulSession, BLANK_NO_PRESENT
    from pre2.enhanced.renderer import EnhancedRenderer
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
    # Live cheap waits: yield the core while parked in a classified retrace wait (live only). On by default;
    # --no-live-cheap-waits forces the original full-spin behavior (a safety/debug switch, not a shim).
    from pre2.recovered.vga_timing import ALL_NODES as _LIVE_RETRACE_NODES
    live_cheap_waits = getattr(args, "live_cheap_waits", True)
    live_active_fraction = float(args.retrace_pulse)
    # Per-kind park diagnostics: "retrace" (9900/990D/44CD) and "pit" (1C6F). `slept`/`unsafe` are shared.
    live_wait_stats = {"retrace": {"parks": 0, "wait_total": 0.0, "wait_max": 0.0},
                       "pit": {"parks": 0, "wait_total": 0.0, "wait_max": 0.0},
                       "slept": 0.0, "unsafe": 0}
    _wait_kind = [None]           # current episode kind ("retrace"/"pit"/None), loop-carried
    _wait_start = [0.0]
    _view_start = perf_counter()  # wall-clock anchor for the CPU-yield estimate

    def _live_wait_kind(rt):      # which classified live wait (if any) the VM is parked in
        if not live_cheap_waits or rt.cpu.s.cs != _LIVE_CS:
            return None
        ip = rt.cpu.s.ip
        if ip in _LIVE_RETRACE_NODES:
            return "retrace"
        if ip in _LIVE_PIT_NODES:
            return "pit"
        return None
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
    # Video backend: 'vm' | 'faithful' | 'enhanced'. 'enhanced' BUILDS ON the faithful backend (it projects
    # the faithful frame through the modern pipeline), so it needs the FaithfulSession too.
    enhanced_mode = getattr(args, "video", "vm") == "enhanced"
    faithful = getattr(args, "video", "vm") in ("faithful", "enhanced")
    faithful_verify = getattr(args, "video_verify", False)   # --video-verify diagnostic

    session = FaithfulSession(rt, args, verify=faithful_verify) if faithful else None
    if session is not None:
        session.install_hooks()
    # The enhanced renderer is a presentation layer ON TOP of the faithful session: it is handed the composed
    # faithful frame + the session's grounded source snapshots (never mem/dos) and projects it through the
    # modern RGB/RGBA object-aware compositor. The session captures a source snapshot per gameplay commit.
    enhanced = None
    if enhanced_mode:
        enhanced = EnhancedRenderer(session, interpolate=not getattr(args, "enhanced_no_interpolation", False))
        session.enhanced_capture = True
        session.enh_clock = perf_counter
        # Threaded present: run the heavy ~17ms extraction on a worker thread (like audio) so the main thread's
        # VM stepping + compose + present never block on it. LIVE only -- demo record/replay stay synchronous
        # (single-threaded, deterministic); async_extract is toggled with `realtime` in the loop below.
        if not replaying:
            session.start_async_extraction()
            session.async_extract = False   # enabled once we confirm we're running live (realtime) below

    def render_current():
        # Faithful backend: FaithfulSession composes the frame from recovered leaves (never the VM
        # framebuffer). VM backend: de-planarize / de-VGA the actual VM video memory. play.py owns only the
        # backend selection + presentation; all faithful capture state/hooks/scene logic lives in the session.
        mem = bytes(rt.program.memory.data)
        if faithful:
            rgb = session.frame(mem)
            faithful_info[0] = session.faithful_info
            if rgb is BLANK_NO_PRESENT:     # display blanked -> keep the previous frame (do NOT present)
                return
            if rgb is None:                 # unknown video mode -> present black
                screen.fill((0, 0, 0))
                pygame.display.flip()
                return
            if enhanced is not None:        # project the faithful frame through the modern pipeline
                rgb = enhanced.present(perf_counter(), rgb)
                faithful_info[0] = f"{faithful_info[0]} | {enhanced.status()}"
        else:
            faithful_info[0] = ""
            mode = rt.dos.video_mode & 0x7F
            if not rt.program.memory.ega_display_enabled:
                faithful_info[0] = "display blanked (palette load)"
                return                      # blanked -> keep the previous frame (matches the original)
            if mode in (0, 1, 2, 3, 7):
                rgb = render_text_rgb(mem, rt.dos.video_mode & 0xFF, rt.dos.video_page)
            elif mode in (0x13, 0x19):
                rgb = render_vga_rgb(mem, rt.dos.vga_palette)
            elif rt.program.memory.ega_planar:
                mem_o = rt.program.memory
                if mem_o.ega_pan_active:
                    ds, pel = mem_o.ega_pan_display_start, mem_o.ega_pan_pel
                else:
                    ds, pel = mem_o.ega_display_start, 0
                active_w = (mem_o.ega_h_display_end + 1) * 8   # CRTC active width (carte = 312, else 320)
                rgb = render_planar_rgb(mem, ds, rt.dos.vga_palette, pel, active_w)
            else:
                screen.fill((0, 0, 0))
                pygame.display.flip()
                return
        last_rgb[0] = rgb
        if _FRAME_HASH:   # extraction-equivalence harness: deterministic per-frame RGB hash (demo replay)
            import hashlib as _fhh
            _h = _fhh.sha1(np.ascontiguousarray(rgb)).hexdigest()[:12] if rgb is not None else "none"
            print(f"[fhash] {frame} {faithful_info[0]!r} {_h}", flush=True)
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
                # Async extraction only while LIVE; a recording (realtime False) uses the synchronous path so
                # the demo stays deterministic (the worker thread idles, fed no snapshots).
                if enhanced is not None and session._extract_thread is not None:
                    session.async_extract = realtime
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
                        # Cheap live waits: if the VM is parked in a classified busy-wait and interrupts are
                        # enabled (so the PIT/SB ISRs can still fire while we yield), poll it with a SMALL
                        # batch and SLEEP instead of spinning. Two kinds, both wall-clock waits in live mode:
                        #   retrace (9900/990D/44CD) -> sleep through the safe interior of the VGA retrace phase
                        #   pit     (1C6F)           -> sleep until the next PIT tick is due (advances [0x27ee])
                        # The VM's own poll still exits at the same wall-clock instant, so pacing is unchanged.
                        kind = _live_wait_kind(rt)
                        safe_park = kind is not None and rt.cpu.get_flag(IF)
                        if _wait_kind[0] is None and kind is not None:          # episode start
                            _wait_kind[0] = kind
                            _wait_start[0] = perf_counter()
                            live_wait_stats[kind]["parks"] += 1
                            if not safe_park:
                                live_wait_stats["unsafe"] += 1
                        if live_cpu_budget is None or frame_steps < live_cpu_budget:
                            n = _LIVE_POLL_BATCH if safe_park else live_irq_batch
                            _pump_and_step(rt, now=perf_counter(), pic=pic, sound_blaster=sound_blaster,
                                           timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                           tick_state=tick_state, n_steps=n)
                            steps_done += n
                            frame_steps += n
                        # else: budget spent — wait out the frame (audio still pumped + yielded below)
                        if _wait_kind[0] is not None and _live_wait_kind(rt) is None:   # episode end
                            dur = perf_counter() - _wait_start[0]
                            st = live_wait_stats[_wait_kind[0]]
                            st["wait_total"] += dur
                            if dur > st["wait_max"]:
                                st["wait_max"] = dur
                            _wait_kind[0] = None
                        # Feed the audio device *continuously* (throttled), not just once
                        # per rendered frame below: the render can stretch a frame past
                        # the mixer channel's buffered depth, leaving its queue slot empty
                        # and underrunning.  Pumping every few ms keeps the slot filled.
                        nowp = perf_counter()
                        if nowp - last_audio >= 0.004:
                            if sb_audio is not None:
                                sb_audio.pump()
                            last_audio = nowp
                        # Yield the core while parked: sleep up to the wait's next wall-clock event (a VGA
                        # retrace edge, or the next PIT tick), minus a margin for OS sleep overshoot, bounded
                        # by the audio-pump cadence and the frame deadline — never sleeping past the event the
                        # VM is waiting for (which would delay it -> change game speed).
                        if safe_park or (live_cpu_budget is not None and frame_steps >= live_cpu_budget):
                            nowp = perf_counter()
                            if kind == "pit":
                                event_dt = (tick_state["next"] - nowp) - _LIVE_PIT_MARGIN
                            else:   # retrace (or budget-exhausted wait-out: use the retrace cadence)
                                event_dt = _time_to_next_retrace_edge(nowp, live_active_fraction) - _LIVE_PARK_MARGIN
                            slice_s = min(event_dt, (last_audio + 0.004) - nowp, deadline - nowp,
                                          _LIVE_PARK_MAX_SLICE)
                            if slice_s >= _LIVE_PARK_MIN_SLICE:
                                sleep(slice_s)
                                live_wait_stats["slept"] += slice_s
                else:
                    chunk = args.chunk_steps if args.steps is None else min(args.chunk_steps, args.steps - steps_done)
                    # Deterministic (demo-replay) clock has a non-zero base here (vclock anchors det_now to
                    # the wall clock at mode switches); pass it so the retrace sampling matches _vga_status.
                    _advance_frame_deterministic(rt, args, chunk_steps=chunk, sub_batch=realtime_batch,
                                                 clock=det_now, pic=pic, sound_blaster=sound_blaster,
                                                 timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                                 tick_state=tick_state, det_speed=det_speed,
                                                 base=vclock["base"])
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
            if _FRAME_HASH and not realtime:
                do_render = True   # harness: render EVERY game-frame so the deterministic replay hash stream is stable
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
        if enhanced is not None:
            session.stop_async_extraction()
        if sb_audio is not None:
            sb_audio.close()
        if getattr(rt, "_verify_summary", None) is not None:
            rt._verify_summary(" final:")
        pygame.quit()

    print(f"status: {status}")
    print(f"frames: {frame}  steps: {steps_done:,}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    print(f"video: mode={rt.dos.video_mode:02X}h text={rt.dos.text_mode_active} page={rt.dos.video_page}")
    if live_cheap_waits and (live_wait_stats["retrace"]["parks"] or live_wait_stats["pit"]["parks"]):
        s = live_wait_stats
        run_s = max(1e-6, perf_counter() - _view_start)
        print(f"live-cheap-waits: slept={s['slept']:.2f}s (~{100.0 * s['slept'] / run_s:.0f}% of "
              f"{run_s:.1f}s wall yielded) unsafe_skipped={s['unsafe']}")
        for kind in ("retrace", "pit"):
            k = s[kind]
            if k["parks"]:
                avg = k["wait_total"] / k["parks"]
                print(f"  {kind:7s} parks={k['parks']} wait avg={avg * 1000:.1f}ms "
                      f"max={k['wait_max'] * 1000:.1f}ms")
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
            _advance_frame_deterministic(rt, args, chunk_steps=chunk, sub_batch=realtime_batch,
                                         clock=det_now, pic=pic, sound_blaster=sound_blaster,
                                         timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                         tick_state=tick_state, det_speed=det_speed)
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
    # The now-driven multi-tick demo clock stores its model knobs (present_hz/retrace_pulse) in the manifest.
    # Demos recorded BEFORE it (no present_hz) used a chunk calibrated for the old 1-tick-per-frame timing and
    # replay in slow motion under the current clock — warn so it isn't mistaken for a performance problem. A
    # demo that carries present_hz replays at its own recorded speed (incl. the default --speed 150000 =>
    # chunk_steps 2142) and must NOT warn — the chunk magnitude alone is not the discriminator, the clock
    # model is.
    if "chunk_steps" in meta and "present_hz" not in meta:
        print("WARNING: this demo predates the now-driven demo clock (no present_hz in its manifest); under "
              "the current clock it may run in slow motion. Re-record it for correct speed.")
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
    # Status/summary lines use a few non-ASCII glyphs (e.g. the '✗' divergence marker in the verify-hooks
    # summary). On a legacy Windows code page (cp1250) the default console encoding raises UnicodeEncodeError
    # mid-print; switch to UTF-8 with a backslash fallback so output is correct on UTF-8 terminals and never
    # crashes on the rest.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
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
    p.add_argument("--video", choices=["vm", "faithful", "enhanced"], default="vm",
                   help="VIDEO BACKEND (a separate axis from the execution mode). "
                        "'vm' (default): display the emulated VM/VGA framebuffer (the original video backend; "
                        "still runs the hybrid native replacements unless --no-replacements). "
                        "'faithful': display the recovered FaithfulVisual backend (render_frame / render_visual "
                        "+ scene leaves, from explicit state + assets); consumes grounded recovered source, "
                        "NEVER reads the VM framebuffer, fails LOUD on an unrecovered scene. "
                        "'enhanced': modern presentation layer ON TOP of the faithful backend (projects the "
                        "faithful frame; never reads the VM framebuffer); currently a passthrough baseline. "
                        "Execution mode is the other axis: hybrid (default) / --no-replacements / --verify-hooks.")
    p.add_argument("--video-verify", action="store_true", help="(with `--video faithful`) each gameplay frame, diff the recovered frame vs the VM's own page over the viewport and show the divergence in the title bar (surfaces any gameplay-state error; small residuals are the live moving-sprite blink-phase)")
    p.add_argument("--enhanced-no-interpolation", action="store_true", help="(with `--video enhanced`) disable object interpolation -> enhanced presents the faithful frame at each source commit (a faithful-equivalent baseline; useful to A/B the interpolation)")
    p.add_argument("--speed", type=int, default=150_000, help="emulated CPU steps/sec for the demo record/replay clock (steps-per-frame = speed/present-hz); the PIT/SB/retrace run at their true rates within that budget. Default 150k ~= PRE2's native rate: its per-frame game work is only ~1.3-1.9k instr (measure_frame_work.py), so ~132k (p90 work x 70Hz) fills one retrace frame with minimal spin; higher values just inflate idle retrace spin (a 450k frame is ~99% spin) and overrun the host interpreter (~270k instr/s) so the demo loop falls behind real time and drops to the 4Hz render fallback. Live --view ignores this and self-paces on the wall clock")
    p.add_argument("--chunk-steps", type=int, default=None, help="override VM steps per frame / demo clock (else derived from --speed and --present-hz)")
    p.add_argument("--present-hz", type=int, default=70, help="live presents per second (also paces the VM to real time); 70 matches the VGA refresh for a smooth present (demos replay at their recorded value)")
    p.add_argument("--retrace-pulse", type=float, default=0.06, help="(live) fraction of each refresh the VGA vertical-retrace status bit reads active. ~0.06 = realistic narrow VGA pulse that gates PRE2's mode-select scroll half-wait to one frame per 70Hz retrace (matches DOSBox); 0.28 = legacy wide window (~2x fast scroll). Demos replay at their recorded value")
    p.add_argument("--cpu-hz", type=int, default=0, help="(live) cap VM instructions/sec as an era-style ceiling (0 = unlimited, today's behavior). Caps ungated busy-loops + sets overall feel; tune by eye against DOSBox (our 'instruction' is not a real CPU cycle)")
    p.add_argument("--live-cheap-waits", action=argparse.BooleanOptionalAction, default=True, help="(live --view) yield the CPU while the VM is parked in a classified VGA retrace busy-wait (9900/990D/44CD): sleep through the safe interior of each retrace phase and busy-poll only the last ~1.5ms before an edge, so the wait still exits at the same wall-clock instant. Same pacing/game timing, less CPU burn/fan/battery. --no-live-cheap-waits forces the original full-spin (safety/debug). Does NOT affect deterministic/headless/verify timing")
    p.add_argument("--fast-adlib", action="store_true", help="mute/skip the hot PRE2 AdLib service thunk: reaches the game fastest, but mutes music")
    p.add_argument("--timer-irq", action=argparse.BooleanOptionalAction, default=True, help="deliver PRE2's INT 08h timer ISR each frame")
    p.add_argument("--input-irq-steps", type=int, default=2_000_000, help="maximum VM steps for one keyboard/timer interrupt")
    p.add_argument("--no-replacements", action="store_true", help="run the pure VM oracle with NO recovered/hybrid hooks (their fixed code/data offsets are bound to one build's layout; use this on a build they weren't derived against)")
    p.add_argument("--verify-hooks", action="store_true", help="run the original ASM as the oracle and diff each recovered-native result against it; prints divergences immediately plus a compact periodic per-hook summary")
    p.add_argument("--verify-verbose", action="store_true", help="(with --verify-hooks) print a line for every OK result, not just divergences + the periodic summary")
    p.add_argument("--full-verify", action="store_true", help="foolproof variant of --verify-hooks: diff the WHOLE machine state (all memory + return cs:ip:sp) after each recovered routine vs the ASM, so nothing can leak outside a hand-picked contract. ~10x slower; for offline snapshot/demo audits, not live play")
    p.add_argument("--trace-hooks", action="store_true", help="run the LIVE hybrid runtime (hooks replacing ASM, NOT the oracle) and show which recovered hooks fire — a live coverage view in the title bar + a periodic/final per-hook tally. Hooks absent = that screen is still pure ASM")
    p.add_argument("--fast-retrace-waits", action=argparse.BooleanOptionalAction, default=True, help="recovered timing primitive (deterministic paths: headless replay, in-view demo replay, verify/oracle): collapse the classified VGA retrace busy-waits (9900/990D/44CD) in closed form, byte-equivalent to the interpreted stepper (~6-15x faster on wait-heavy scenes). On by default with the hybrid runtime; --no-fast-retrace-waits forces the pure interpreted ASM loops (and it is off under --no-replacements). Does NOT affect live --view wall-clock pacing")
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
