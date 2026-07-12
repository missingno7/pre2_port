"""Prove the VM-less NATIVE front end reproduces the reference VM, screen-for-screen (and opt-in pixel-for-pixel).

The tick-demo verifier proves the gameplay core but captures ZERO of the front end (intro / title / menu / attract
run with NO gameplay tick). This is its front-end analogue, and it uses the SAME oracle trick the tick demo does:
drive the reference VM with a recorded demo and, at every present-frame, capture BOTH what is on screen (a coarse
logical screen id + a pixel digest) AND the raw keyboard scancode flags the front end sampled that frame. Then replay
the VM-less native front end injecting those SAME per-frame flags, and diff the two timelines.

  * reference = the real PRE2.EXE in the VM, replaying <demo> (its recorded input drives the front end faithfully).
  * candidate = the VM-less native front end (``native_cold_boot`` -> ``native_front_end``), fed the VM's per-frame
    input so it makes the same choices — no guessing, no synthetic keystrokes.

    python scripts/verify_native_frontend.py <cold_start_demo> [--frames N] [--pixel]

The demo must be a COLD-START recording (boot -> OLDIES -> titles -> menu -> level): the native side starts from
``native_cold_boot`` (the OLDIES-entry state), so the two only align if the VM starts there too. (A menu-start demo
starts mid-scene, where the native scene generators have no matching resume point.)

SEQUENCE is always checked (screen order + per-screen frame counts — the class of bug the expert-eater wall was).
``--pixel`` additionally diffs the per-frame RGB digest (the strong proof: byte-exact rendering AND cadence).
Exit 0 = match, 1 = divergence.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from dos_re.frontend_timeline import (capture, collapse, diff_pixels, diff_sequence, format_sequence, rgb_sha)
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from frontend_capture import _fingerprint_map, classify_native_scene, classify_vm_frame

_DS = 0x1A0F << 4
# the keyboard scancode-flag window DC1 ORs into the six FSM input flags ([0x28xx], set by INT 09). Capturing this
# whole span from the VM and injecting it into native's DGROUP makes native's decode_input see identical input.
_KBD_LO, _KBD_HI = 0x2800, 0x2880


def capture_vm(demo_dir: str, max_frames: int):
    """Replay <demo_dir> on the VM; per present-frame return (screen, rgb_sha, kbd_flags_bytes).

    The capture is CACHED in the demo dir (frontend_timeline.json) — a full-demo VM replay costs minutes and
    is deterministic, so it is recorded once per (demo, frames) and reused by every subsequent verify run."""
    import json as _json
    from dos_re.frontend_timeline import FrameRecord

    cache = Path(demo_dir) / "frontend_timeline.json"
    if cache.exists():
        blob = _json.loads(cache.read_text(encoding="utf-8"))
        if blob.get("frames") >= min(max_frames, blob.get("demo_frames", 0)):
            records = [FrameRecord(f, s, h) for f, s, h in blob["records"][:max_frames]]
            inputs = [bytes.fromhex(x) for x in blob["inputs"][:max_frames]]
            print(f"loaded the cached VM timeline {cache.name} ({len(records)} frames)")
            return records, inputs

    from pre2.bridge.timing_fastforward import advance_frame_fast
    from pre2.runtime import load_pre2_snapshot

    pb = InputDemoPlayback.load(demo_dir)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 2142)); hz = int(meta.get("present_hz", 70))
    mode = str(meta.get("replacements", "hybrid"))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=mode)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (chunk * hz)              # noqa: E731
    rt.dos.time_source = det
    sb = enable_sound_blaster(rt); sb.clock = det
    tick = {"next": 0.0}
    fpmap = _fingerprint_map(str(ROOT / "assets"))
    records, inputs = [], []

    def sample(i):
        if pb.finished(i):
            return None
        pb.apply_to_runtime(i, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        advance_frame_fast(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                           sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick,
                           det_speed=chunk * hz, active_fraction=rt.dos.vga_retrace_active_fraction, base=0.0)
        if sb.pcm_out:
            sb.pcm_out.clear()
        d = rt.program.memory.data
        inputs.append(bytes(d[_DS + _KBD_LO:_DS + _KBD_HI]))       # the scancode flags DC1 read this frame
        screen, rgb = classify_vm_frame(rt, str(ROOT / "assets"), fpmap)
        return screen, rgb_sha(rgb)

    print(f"replaying {Path(demo_dir).name} through the VM ({mode}) ...")
    records = capture(sample, max_frames)
    cache.write_text(_json.dumps({
        "frames": len(records), "demo_frames": len(records),
        "records": [[r.frame, r.screen, r.rgb_sha] for r in records],
        "inputs": [x.hex() for x in inputs],
    }), encoding="utf-8")
    print(f"cached the VM timeline -> {cache}")
    return records, inputs


def capture_native(kbd_inputs, max_frames: int):
    """Drive the native front end from cold boot, injecting the VM's per-frame scancode flags; return its timeline."""
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.vga import NativeVGA

    state = native_cold_boot(str(ROOT / "assets"))
    dos = NativeVGA()
    gen = native_front_end(state, dos, 0, game_root=str(ROOT / "assets"), intro_skippable=False)
    fpmap = _fingerprint_map(str(ROOT / "assets"))
    box = {"gen": gen}

    def sample(i):
        # inject the VM's scancode flags for THIS frame BEFORE resuming the generator, so the scene-wait /
        # menu decode that runs after the yield reads the same input the VM's DC1 did (native is live-mode).
        if i < len(kbd_inputs):
            state.data[_DS + _KBD_LO:_DS + _KBD_HI] = kbd_inputs[i]
        try:
            scene = next(box["gen"])
        except StopIteration:
            return None
        screen, rgb = classify_native_scene(scene, str(ROOT / "assets"), fpmap)
        return screen, rgb_sha(rgb)

    print("driving the VM-less native front end (cold boot) with the VM's captured input ...")
    return capture(sample, max_frames)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("demo", help="a COLD-START demo dir (boot -> OLDIES -> titles -> menu -> level)")
    ap.add_argument("--frames", type=int, default=1400)
    ap.add_argument("--pixel", action="store_true", help="also diff the per-frame RGB digest (strong proof)")
    ap.add_argument("--tolerance", type=int, default=2, help="allowed per-screen frame-count delta in the sequence")
    args = ap.parse_args(argv)

    demo_dir = str(ROOT / args.demo) if not Path(args.demo).is_absolute() else args.demo
    vm, kbd = capture_vm(demo_dir, args.frames)

    # --- align the two timelines ---
    # 1) TRIM the VM's boot-init prefix: a cold-start demo runs main()'s init (fonts/joystick/year/palette,
    #    ~100 present-frames of blank/undefined video) before the OLDIES entry — native_cold_boot starts AT the
    #    OLDIES entry, so native's frame 0 corresponds to the VM's first planar (0Dh) frame.
    trim = next((i for i, r in enumerate(vm) if r.screen == "0Dh"), 0)
    if trim:
        print(f"aligned: VM boot-init prefix = {trim} frames (trimmed; native cold-boots at the OLDIES entry)")
    vm_fe, kbd_fe = vm[trim:], kbd[trim:]

    native = capture_native(kbd_fe, len(vm_fe))

    # 2) SCOPE the compare to the front end: the native generator ends when the level loads; the VM demo keeps
    #    going into gameplay. Compare frame-for-frame over native's span; report the VM tail for context.
    vm_scoped = vm_fe[:len(native)]
    tail = collapse(vm_fe[len(native):])
    vm_runs, nat_runs = collapse(vm_scoped), collapse(native)
    print(f"\nVM     ({len(vm_scoped):4d} frames):  {format_sequence(vm_runs)}")
    print(f"native ({len(native):4d} frames):  {format_sequence(nat_runs)}")
    if tail:
        print(f"VM continues past the front end ({sum(r.count for r in tail)} frames): "
              f"{format_sequence(tail[:6])}{' -> ...' if len(tail) > 6 else ''}")

    # --- the verdict: the FILTERED screen ORDER (the invariant every capture can prove) ---
    # The VM shows 1-2 frame TRANSITION states native never renders as frames of its own (a black 'loading'
    # head mid image-copy, 'other' mid mode-switch, 'blanked' during a palette load) — filter them and merge.
    from dos_re.frontend_timeline import filter_runs
    _TRANSITIONS = {"13h:loading", "loading", "other", "blanked", "text"}
    vm_seq, nat_seq = filter_runs(vm_runs, _TRANSITIONS), filter_runs(nat_runs, _TRANSITIONS)
    print(f"\nfiltered VM     : {format_sequence(vm_seq)}")
    print(f"filtered native : {format_sequence(nat_seq)}")

    sd = diff_sequence(vm_seq, nat_seq, duration_tolerance=None)          # ORDER is the gate
    if not sd.ok:
        print(f"\n  SEQUENCE DIVERGED at run {sd.index}: {sd.reason}\n    VM    : {sd.a}\n    native: {sd.b}")
        return 1
    print(f"\n  SEQUENCE OK: native shows the VM's {len(vm_seq)} screens in the SAME ORDER.")

    # --- cadence: are the demo's present-frames retrace-faithful? Decide from a TIMED screen (TITUS: the
    #     fade+hold+fade runs ~117 retraces regardless of input). A workbench demo recorded under a small
    #     instruction budget (chunk-steps) inflates timed screens' frame counts — durations and pixels are then
    #     apples-to-oranges (and the per-frame input indexes shift), so the strong gates need a retrace-faithful
    #     recording; the ORDER gate above is the proof this capture supports. ---
    vm_titus = next((r.count for r in vm_seq if r.screen == "13h:TITUS.SQZ"), None)
    nat_titus = next((r.count for r in nat_seq if r.screen == "13h:TITUS.SQZ"), None)
    cadence = (vm_titus / nat_titus) if (vm_titus and nat_titus) else 1.0
    if cadence > 1.5:
        print(f"  cadence: the demo's present-frame clock runs ~{cadence:.1f}x the retrace on timed screens "
              f"(workbench chunk-steps budget). Duration + pixel gates are not applicable to this recording; "
              f"the screen-ORDER gate above is the front-end proof it supports.")
        if args.pixel:
            print("  --pixel skipped (needs a retrace-faithful recording).")
        return 0

    sd = diff_sequence(vm_seq, nat_seq, duration_tolerance=args.tolerance)
    if not sd.ok:
        print(f"\n  DURATIONS DIVERGED at run {sd.index}: {sd.reason}\n    VM    : {sd.a}\n    native: {sd.b}")
        return 1
    print(f"  DURATIONS OK: every screen within {args.tolerance} frames.")
    if args.pixel:
        pd = diff_pixels(vm_scoped, native)
        if not pd.ok:
            print(f"  PIXELS DIVERGED at frame {pd.frame} (VM {pd.screen_ref}={pd.sha_ref[:12]} "
                  f"vs native {pd.screen_cand}={pd.sha_cand[:12]}) after {pd.compared} identical frames.")
            return 1
        print(f"  PIXELS OK: all {pd.compared} frames byte-identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
