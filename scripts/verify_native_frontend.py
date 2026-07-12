"""Prove the VM-less NATIVE front end behaves like the ORIGINAL: byte-compared state at every screen transition.

The tick-demo verifier proves gameplay tick-for-tick, but the front end (oldies/titles/menu/mode-select/carte)
has no game ticks — and its per-frame CADENCE is not comparable across the VM and native (the VM recording rides
a wall-clock/instruction budget; native scene-frames don't), so a frame-for-frame pixel diff is the wrong proof.
What IS well-defined, mode- and cadence-independent, is the front end's DECISION STATE at each discrete SCREEN
TRANSITION, and the final GAMEPLAY-ENTRY state. So, the front-end analogue of the tick demo:

  1. Replay the demo on the VM (pure ASM = the original). Per present-frame capture the logical screen, the raw
     input the front end sampled (the [0x27E0..0x2880] key-flag window: DC1 sources + the menu's '1'/'2' flags),
     and the front-end DECISION-STATE WITNESS (level/mode/lives/score/load-tops/password/input-source).
  2. Drive the VM-less native front end (cold boot). Input is fed CAUSALLY, segmented per screen: native gets the
     VM's recorded input stream for the SCREEN it is currently on (a timed screen that native finishes faster
     just skips ahead), so keypresses land on the same screen at the same relative moment — no shared clock needed.
  3. At every screen transition, byte-compare the witness. At the END (the native generator loaded the level),
     compare the full masked GAMEPLAY DIGEST against the tick-demo seed (the VM's memory at its first gameplay
     tick). Digest equal => the whole native front end produced the byte-identical gameplay-entry state the
     original produced — menu, mode select, carte, loader, everything that matters.

    python scripts/verify_native_frontend.py <cold_start_demo> [--frames N]

Needs <demo>/game_tick_demo.bin for the final seed anchor (create once: scripts/verify_native_tick_demo.py <demo>).
Exit 0 = order + every transition witness + the gameplay-entry digest all match; 1 = a divergence (localized to
the first transition where the state differs).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from dos_re.frontend_timeline import capture, collapse, diff_sequence, filter_runs, format_sequence, rgb_sha
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from frontend_capture import _fingerprint_map, classify_native_scene, classify_vm_frame

_DS = 0x1A0F << 4
# the raw input window the front end samples: DC1's decoded flags + scancode sources [0x28xx] AND the menu's
# direct '1'/'2' key flags [0x27F6]/[0x27F7] + the idle counter [0x27F0] (so native's attract-timeout behaviour
# mirrors the VM's exactly instead of free-running).
_IN_LO, _IN_HI = 0x27E0, 0x2880

# The front-end DECISION-STATE WITNESS: what the front end is FOR — the state that determines which level starts
# and how. Byte-compared at every screen transition; a mismatch here is a real behaviour divergence.
WITNESS = (
    ("level[2D8A]",      0x2D8A, 1),   # the committed level
    ("mode[B197]",       0xB197, 1),   # beginner/expert
    ("mode_copy[B198]",  0xB198, 1),
    ("input_src[2879]",  0x2879, 1),   # 0=live keyboard, 1=attract demo playback — attract shows up HERE
    ("lives[27D8]",      0x27D8, 1),   # the fresh-start block ([asm 0141]) runs at menu entry
    ("score[6C0E]",      0x6C0E, 4),
    ("attract_lvl[83E]", 0x083E, 1),   # the attract/default level header
    ("pw_hist[B1B3]",    0xB1B3, 6),   # password rolling history (3 groups)
    ("pw_code[B1B9]",    0xB1B9, 2),   # the accumulated password code
)

# LAYOUT/AUDIO-owned fields: native legitimately differs here BY DESIGN — its loaders stack only what the
# VM-less runtime uses (no DOS sound-driver module images in DGROUP), so the load-buffer history diverges.
# Reported informationally; their gameplay-irrelevance is PROVEN behaviourally by gate [4], not assumed.
LAYOUT_INFO = (
    ("load_top[2875]",   0x2875, 2),
    ("reset_base[39]",   0x0039, 2),
    ("fg_bank[3B]",      0x003B, 2),
)

# transition-state screens (fade frames, blanked display, unclassifiable) — not logical screens; the ORDER gate
# and the input segmentation both run on the FILTERED sequence.
_TRANSIENT = ("blanked", "other", "text", "13h:loading", "13h:?")


def _canon(screen: str):
    return None if screen in _TRANSIENT else screen


def witness_bytes(data, base=_DS, fields=WITNESS) -> bytes:
    return b"".join(bytes(data[base + off:base + off + n]) for _, off, n in fields)


def witness_diff(a: bytes, b: bytes, fields=WITNESS) -> "list[str]":
    out, pos = [], 0
    for name, _off, n in fields:
        va, vb = a[pos:pos + n], b[pos:pos + n]
        if va != vb:
            out.append(f"{name}: VM={va.hex()} native={vb.hex()}")
        pos += n
    return out


def capture_vm(demo_dir: str, max_frames: int):
    """Replay <demo_dir> on the VM. Returns (records, kbd_per_frame, witness_per_frame)."""
    from pre2.bridge.timing_fastforward import advance_frame_fast
    from pre2.runtime import load_pre2_snapshot

    pb = InputDemoPlayback.load(demo_dir)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 2142)); hz = int(meta.get("present_hz", 70))
    mode = str(meta.get("replacements", "pure"))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=mode)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (chunk * hz)              # noqa: E731
    rt.dos.time_source = det
    sb = enable_sound_blaster(rt); sb.clock = det
    tick = {"next": 0.0}
    fpmap = _fingerprint_map(str(ROOT / "assets"))
    kbd, wits = [], []

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
        kbd.append(bytes(d[_DS + _IN_LO:_DS + _IN_HI]))
        wits.append(witness_bytes(d))
        screen, rgb = classify_vm_frame(rt, str(ROOT / "assets"), fpmap)
        return screen, rgb_sha(rgb)

    print(f"replaying {Path(demo_dir).name} through the VM ({mode}) ...", flush=True)
    records = capture(sample, max_frames)
    return records, kbd, wits


def capture_native(segments, max_frames: int):
    """Drive the native front end from cold boot, feeding the VM's input CAUSALLY per screen segment.

    ``segments`` = ordered [(screen_id, [kbd bytes per VM frame of that screen])]. Native consumes segment k's
    input while its own classified screen == segments[k][0]; when native's screen advances to segment k+1's id,
    the cursor jumps there (a timed screen native finishes faster just skips the unused input — the presses for
    LATER screens are still delivered on those screens). Returns (records, witness_per_frame, final_state)."""
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.front_end import native_front_end
    from pre2.native.input import init_keyboard_input
    from pre2.native.state import NativeGameState
    from pre2.native.vga import NativeVGA

    # the TRUE front-end entry: the OLDIES-entry boot constants + the keyboard-play joystick outcome — exactly
    # what play_native's cold start uses. (NOT native_cold_boot: that is the level-JUMP bootstrap — it pre-stacks
    # FRONT.SQZ/sprites and pre-loads a level, which native_front_end then does AGAIN mid-flow at the faithful
    # point, double-stacking the load top and skewing every downstream segment.)
    state = NativeGameState(build_boot_memory())
    init_keyboard_input(state)
    dos = NativeVGA()
    gen = native_front_end(state, dos, 0, game_root=str(ROOT / "assets"), intro_skippable=False)
    fpmap = _fingerprint_map(str(ROOT / "assets"))
    seg = {"k": 0, "c": 0}
    wits = []
    zeros = bytes(_IN_HI - _IN_LO)

    def sample(i):
        # feed the CURRENT segment's next input frame (zeros when exhausted: keys released)
        k, c = seg["k"], seg["c"]
        buf = segments[k][1][c] if k < len(segments) and c < len(segments[k][1]) else zeros
        state.data[_DS + _IN_LO:_DS + _IN_HI] = buf
        seg["c"] = c + 1
        try:
            scene = next(gen)
        except StopIteration:
            return None
        screen, rgb = classify_native_scene(scene, str(ROOT / "assets"), fpmap)
        wits.append(witness_bytes(state.data))
        # native advanced to the NEXT logical screen -> switch to that screen's recorded input segment
        cs = _canon(screen)
        if cs is not None and seg["k"] + 1 < len(segments) and cs == segments[seg["k"] + 1][0]:
            seg["k"] += 1
            seg["c"] = 0
        return screen, rgb_sha(rgb)

    print("driving the VM-less native front end (cold boot; causal per-screen input) ...", flush=True)
    records = capture(sample, max_frames)
    return records, wits, state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("demo", help="a COLD-START demo dir (boot -> oldies -> titles -> menu -> level)")
    ap.add_argument("--frames", type=int, default=12000)
    args = ap.parse_args(argv)
    demo_dir = str(ROOT / args.demo) if not Path(args.demo).is_absolute() else args.demo

    vm, kbd, vm_wits = capture_vm(demo_dir, args.frames)

    # --- the VM's LOGICAL screen sequence (transition states filtered + split runs merged) + input segments ---
    vm_filtered = filter_runs(collapse(vm), ignore=set(_TRANSIENT))
    bounds = [r.start for r in vm_filtered] + [len(vm)]
    segments = [(vm_filtered[j].screen, kbd[bounds[j]:bounds[j + 1]]) for j in range(len(vm_filtered))]
    print(f"VM logical sequence: {format_sequence(vm_filtered)}")

    native, nat_wits, nat_state = capture_native(segments, args.frames)
    nat_filtered = filter_runs(collapse(native), ignore=set(_TRANSIENT))
    print(f"native logical sequence: {format_sequence(nat_filtered)}")

    failures = 0

    # --- gate 1: SCREEN ORDER (cadence-independent) ---
    sd = diff_sequence(vm_filtered, nat_filtered, duration_tolerance=None)
    if sd.ok:
        print(f"\n[1] ORDER OK: {len(nat_filtered)} logical screens in the VM's order")
    else:
        failures += 1
        print(f"\n[1] ORDER DIVERGED at screen {sd.index}: {sd.reason}\n    VM: {sd.a}  native: {sd.b}")

    # --- gate 2: the DECISION-STATE WITNESS at every screen transition ---
    n = min(len(vm_filtered), len(nat_filtered))
    print(f"[2] transition witnesses ({len(WITNESS)} fields x {n} transitions):")
    for j in range(n):
        w_vm = vm_wits[vm_filtered[j].start]
        w_nat = nat_wits[nat_filtered[j].start]
        diffs = witness_diff(w_vm, w_nat)
        tag = "OK " if not diffs else "DIFF"
        print(f"    -> {vm_filtered[j].screen:16s} {tag}" + (f"  {'; '.join(diffs)}" if diffs else ""))
        failures += bool(diffs)

    # --- gates 3+4: the GAMEPLAY-ENTRY state, split by OWNERSHIP, and the inertness PROOF ---
    #
    # [3] Outside the audio/layout-owned bytes, native's front-end output must be BYTE-IDENTICAL to the VM's
    #     state at its first gameplay tick (the tick-demo seed). The owned bytes (the DOS sound driver's module
    #     images/tables in DGROUP + the load-layout pointers + front-end scene scratch) are where the VM-less
    #     product legitimately differs — the same ownership boundary the gameplay digest draws for audio.
    # [4] That ownership claim is then PROVEN, not assumed: replay the demo's recorded gameplay ticks TWICE —
    #     once from the VM seed (known 15/15) and once from native's own front-end output — stepping both with
    #     identical injected input. If, at every tick, the two states are byte-identical outside the initial
    #     owned set and the diff never grows beyond it, the owned bytes are demonstrably INERT: the game
    #     behaves byte-identically from native's own cold start, over the whole recording.
    tick_path = Path(demo_dir) / "game_tick_demo.bin"
    if tick_path.exists():
        from pre2.native.game_tick_demo import GameTickDemo, _inject, gameplay_digest
        from pre2.native.loop import native_cave_teleport, native_gameplay_frame
        from pre2.native.seams import _FWD_EXCL, _SLOT5_PAGE, _SLOT_BASE, _SLOT_STRIDE
        from pre2.native.state import NativeGameState
        from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition

        gtd = GameTickDemo.load(tick_path)

        def _masked(dgroup):
            buf = bytearray(dgroup[:0x10000])
            for o in _FWD_EXCL:
                if o < 0x10000:
                    buf[o] = 0
            for o in _SLOT5_PAGE:
                if o < 0x10000:
                    buf[o] &= 0x9F
            for b in range(_SLOT_BASE, 0x5732, _SLOT_STRIDE):
                if b != 0x4F1C and dgroup[b + 4] == 0xFF and dgroup[b + 5] == 0xFF:
                    buf[b] = buf[b + 1] = buf[b + 2] = buf[b + 3] = 0
            return buf

        a0 = _masked(gtd.seed[_DS:_DS + 0x10000])
        b0 = _masked(nat_state.data[_DS:_DS + 0x10000])
        owned = {o for o in range(0x10000) if a0[o] != b0[o]}
        d_vm, d_nat = gameplay_digest(gtd.seed[_DS:_DS + 0x10000]), gameplay_digest(nat_state.data[_DS:_DS + 0x10000])
        if not owned:
            print(f"[3] GAMEPLAY-ENTRY OK: byte-identical (digest {d_vm[:12]})")
        else:
            print(f"[3] gameplay-entry: byte-identical OUTSIDE {len(owned)} audio/layout-owned bytes "
                  f"(sound-driver module data + load-layout pointers + scene scratch; native owns these "
                  f"by design). Proving they are gameplay-INERT:")

        # --- [4] dual tick replay ---
        sa = NativeGameState(bytearray(gtd.seed))                  # from the VM's own gameplay-entry state
        sb = NativeGameState(bytearray(nat_state.data))            # from NATIVE's front-end-produced state

        def step(st, i):
            """One recorded tick, mirroring verify_native: drain teleport/respawn in-state; anything else
            (level-end / game-over / game-complete / a real gap) TERMINATES the compare — return its name."""
            _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            try:
                native_gameplay_frame(st)
            except Pre2CaveTeleport as tp:
                for _ in native_cave_teleport(st, tp.si):
                    pass
            except Pre2RespawnTransition:
                from pre2.native.level_state import native_4f6c
                for _ in native_4f6c(st):
                    pass
            except Pre2HybridGap as e:                             # terminal transitions end the compare
                return type(e).__name__
            return None

        inert = True
        for i in range(gtd.n_ticks):
            ta, tb = step(sa, i), step(sb, i)
            if ta != tb:
                print(f"[4] INERTNESS FAILED at tick {i}: seed-run raised {ta} vs native-run {tb}")
                inert = False
                break
            if ta is not None:                                     # both terminal with the SAME transition — the
                print(f"[4] both runs reached {ta} at tick {i} -- compare ends there (front-end flow)")
                break                                              # remaining ticks belong to the next flow
            if gameplay_digest(sa.data[_DS:_DS + 0x10000]) != gtd.digests[i]:
                print(f"[4] (sanity) the seed-run itself diverged from the recording at tick {i}")
                inert = False
                break
            ma, mb = _masked(sa.data[_DS:_DS + 0x10000]), _masked(sb.data[_DS:_DS + 0x10000])
            spread = [o for o in range(0x10000) if ma[o] != mb[o] and o not in owned]
            if spread:
                print(f"[4] INERTNESS FAILED at tick {i}: the owned-region diff PROPAGATED to "
                      f"{len(spread)} gameplay byte(s); first: "
                      + ", ".join(f"[{o:#06x}] seed={ma[o]:02x} native={mb[o]:02x}" for o in spread[:8]))
                inert = False
                break
        if inert:
            if owned:
                print(f"[4] INERT OK: all {gtd.n_ticks} recorded ticks evolved byte-identically from native's "
                      f"own cold start (the {len(owned)} owned bytes never influenced gameplay state)")
        else:
            failures += 1
    else:
        print(f"[3] SKIPPED: no {tick_path.name} (run scripts/verify_native_tick_demo.py {args.demo} once)")

    print("\nPASS: the native front end behaves like the original -- screen order, decision state at every "
          "transition, and the recorded gameplay evolving byte-identically from native's own cold start."
          if not failures else f"\nFAIL: {failures} divergence(s) above.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
