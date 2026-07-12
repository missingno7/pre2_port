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
    """Replay <demo_dir> on the VM; per present-frame return (screen, rgb_sha, kbd_flags_bytes)."""
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
    native = capture_native(kbd, args.frames)
    vm_runs, nat_runs = collapse(vm), collapse(native)
    print(f"\nVM     ({len(vm):4d} frames):  {format_sequence(vm_runs)}")
    print(f"native ({len(native):4d} frames):  {format_sequence(nat_runs)}")

    sd = diff_sequence(vm_runs, nat_runs, duration_tolerance=args.tolerance)
    if not sd.ok:
        print(f"\n  SEQUENCE DIVERGED at run {sd.index}: {sd.reason}\n    VM    : {sd.a}\n    native: {sd.b}")
        return 1
    print(f"\n  SEQUENCE OK: native reproduced the VM's {len(vm_runs)} screens in order "
          f"(each within {args.tolerance} frames).")
    if args.pixel:
        pd = diff_pixels(vm, native)
        if not pd.ok:
            print(f"  PIXELS DIVERGED at frame {pd.frame} (VM {pd.screen_ref}={pd.sha_ref[:12]} "
                  f"vs native {pd.screen_cand}={pd.sha_cand[:12]}) after {pd.compared} identical frames.")
            return 1
        print(f"  PIXELS OK: all {pd.compared} frames byte-identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
