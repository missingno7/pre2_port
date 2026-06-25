"""Lockstep equivalence of the recovered retrace fast-forward primitive vs the interpreted PRODUCTION step.

Two independent runtimes from the same snapshot, set up identically (same SB, deterministic clock,
tick_state, active_fraction):
  * REFERENCE: driven by the real ``scripts/play._advance_demo_frame`` (interprets every instruction).
  * FAST:      driven by ``pre2.bridge.timing_fastforward.advance_frame_fast`` (collapses the classified
               9900/990D/44CD retrace polls between sub_batch boundaries).
Both are warmed with the SAME (reference) driver to reach the scene, then run N frames each with its own
driver, asserting BYTE-IDENTICAL state at every frame boundary: whole memory + all registers +
instruction_count. (Zero diffs is the bar — the fast path is meant to reproduce the existing timeline
exactly, so no demo re-recording is required.) Also reports the host-time saving.
"""
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import play  # noqa: E402  (production deterministic demo stepper — the reference)
from dos_re.runtime import enable_sound_blaster  # noqa: E402
from pre2.bridge.timing_fastforward import advance_frame_fast  # noqa: E402
from pre2.runtime import load_pre2_snapshot  # noqa: E402

CHUNK = 6428
PRESENT_HZ = 70
SUB_BATCH = 2000
ACTIVE_FRACTION = 0.06
INPUT_IRQ_STEPS = 2_000_000
_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es", "ss", "cs", "ip", "flags")


def _setup(snap):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    rt.cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    det_speed = CHUNK * PRESENT_HZ
    det_now = lambda: rt.cpu.instruction_count / det_speed  # noqa: E731
    rt.dos.time_source = det_now
    rt.dos.vga_retrace_active_fraction = ACTIVE_FRACTION
    return {"rt": rt, "sb": sb, "det_speed": det_speed, "clock": det_now,
            "tick": {"next": 0.0}, "det_now": det_now}


def _ref_frame(c):
    play._advance_demo_frame(c["rt"], chunk_steps=CHUNK, sub_batch=SUB_BATCH, clock=c["clock"],
                             pic=c["rt"].dos.pic, sound_blaster=c["sb"], timer_irq=True,
                             input_irq_steps=INPUT_IRQ_STEPS, tick_state=c["tick"])


def _fast_frame(c):
    advance_frame_fast(c["rt"], chunk_steps=CHUNK, sub_batch=SUB_BATCH, clock=c["clock"],
                       pic=c["rt"].dos.pic, sound_blaster=c["sb"], timer_irq=True,
                       input_irq_steps=INPUT_IRQ_STEPS, tick_state=c["tick"],
                       det_speed=c["det_speed"], active_fraction=ACTIVE_FRACTION)


def run(snap, warm_frames, cmp_frames):
    ref = _setup(snap)
    fst = _setup(snap)
    for c in (ref, fst):                    # identical warm-up with the reference driver
        for _ in range(warm_frames):
            _ref_frame(c)
    if bytes(ref["rt"].cpu.mem.data) != bytes(fst["rt"].cpu.mem.data):
        print("WARN: runtimes diverged during the (identical) warm-up — setup non-determinism")

    bad = 0
    t_ref = t_fast = 0.0
    for f in range(cmp_frames):
        t0 = time.perf_counter(); _ref_frame(ref); t_ref += time.perf_counter() - t0
        t0 = time.perf_counter(); _fast_frame(fst); t_fast += time.perf_counter() - t0
        a, b = ref["rt"].cpu.mem.data, fst["rt"].cpu.mem.data
        md = [i for i in range(len(a)) if a[i] != b[i]]
        rd = {r: (getattr(ref["rt"].cpu.s, r), getattr(fst["rt"].cpu.s, r)) for r in _REGS
              if getattr(ref["rt"].cpu.s, r) != getattr(fst["rt"].cpu.s, r)}
        icd = ref["rt"].cpu.instruction_count != fst["rt"].cpu.instruction_count
        if md or rd or icd:
            bad += 1
            if bad <= 3:
                print(f"  frame {f}: mem_diffs={len(md)} first={hex(md[0]) if md else None} regs={rd} "
                      f"ic_ref={ref['rt'].cpu.instruction_count} ic_fast={fst['rt'].cpu.instruction_count}")
    return bad, cmp_frames, t_ref, t_fast


def main():
    total_bad = 0
    for label, snap, warm in (
        ("MAP/CARTE 990D", "artifacts/snapshot_pre2_mapscroll_20260623_110253", 4),
        ("MENU 9900", "artifacts/snapshot_pre2_modeselect_20260623_075918", 4),
        ("GAMEPLAY 44CD", "artifacts/snapshot_pre2_gameplay_20260621_185902", 4),
    ):
        bad, n, t_ref, t_fast = run(snap, warm_frames=warm, cmp_frames=80)
        speed = (t_ref / t_fast) if t_fast else 0.0
        print(f"=== {label} ===  frames={n} bad={bad}  ref={t_ref:.2f}s fast={t_fast:.2f}s  speedup={speed:.1f}x")
        total_bad += bad
    print("FAST-RETRACE vs PRODUCTION _advance_demo_frame:",
          "PASS" if total_bad == 0 else f"FAIL ({total_bad} divergent frames)")
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
