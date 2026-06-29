"""Whole-routine shadow of native_camera_follow (the VM-less 5643 camera-follow) vs the ASM 5643->5662.

At 5643 entry: seed a NativeGameState from the pre-state, run native_camera_follow (no VM), capture its predicted
DGROUP. At the 5662 RET: diff predicted vs the ASM-mutated DGROUP, excluding async audio/ISR scratch. The plane
redraw writes VRAM (invisible to a DGROUP diff). native_replacements=False so the ASM is the pure oracle.

Run: python -m pre2.probes.probe_camera_scroll
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot
from pre2.native.state import DATA_SEG, NativeGameState
from pre2.native.camera_scroll import native_camera_follow
import play

CS = 0x1030
ENTRY = (CS, 0x5643)
RET = (CS, 0x5662)
_BASE = (DATA_SEG << 4) & 0xFFFFF
_EXCL = ({0x27EE, 0x27EF, 0x1004, 0x1005, 0x1006, 0x1007, 0x2841, 0x2874, 0x2DF6, 0x2DF7}
         | set(range(0x27F0, 0x2800)) | set(range(0xAB0, 0xE00)))
_WATCH = (0x4F1E, 0x4F2A, 0x2DE6, 0x8166, 0x6BF1, 0x6BEE, 0x6BC4, 0x815E, 0x2CF5, 0x2DF4)


def _run(demo, max_frames, stats, mism):
    pb = InputDemoPlayback.load(demo)
    rt = load_pre2_snapshot(str(ROOT / "assets" / "pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; md = cpu.mem.data
    pend = {}

    def _rw(o):
        b = _BASE + o
        return md[b] | (md[b + 1] << 8)

    def at_entry(c):
        state = NativeGameState(bytearray(md))
        pend["watch"] = {o: _rw(o) for o in _WATCH}
        try:
            native_camera_follow(state)
            pend["pred"] = bytes(state.data[_BASE:_BASE + 0x10000])
            stats["call"] += 1
        except Exception as e:                                   # noqa: BLE001
            stats[f"ERR:{type(e).__name__}"] += 1
            if len([m for m in mism if m.startswith("ERR")]) < 3:
                mism.append(f"ERR {Path(demo).name}: {type(e).__name__}: {str(e)[:70]}")
        from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
        interpret_current_instruction_without_hook(c)

    def at_ret(c):
        pred = pend.pop("pred", None)
        if pred is not None:
            post = md[_BASE:_BASE + 0x10000]
            bad = [o for o in range(0x10000) if post[o] != pred[o] and o not in _EXCL]
            if bad:
                stats["BAD"] += 1
                if stats["BAD"] <= 4:
                    w = pend.get("watch", {})
                    mism.append("  IN " + " ".join(f"{o:#06x}={w[o]:#06x}" for o in _WATCH))
                    mism.append("  OUT " + " ".join(f"[{o:#06x}]n{pred[o]:#04x}/v{post[o]:#04x}" for o in bad[:10]))
            else:
                stats["ok"] += 1
        from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[ENTRY] = at_entry; cpu.hook_names[ENTRY] = "cam_entry"
    cpu.replacement_hooks[RET] = at_ret; cpu.hook_names[RET] = "cam_ret"

    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < max_frames:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        frame += 1
    print(f"  {Path(demo).name}: {frame} frames")


def main():
    demos = [("artifacts/demo_pre2_20260629_141422", 800),
             ("artifacts/demo_pre2_full_gorilla_20260628_203423", 900),
             ("artifacts/demo_pre2_20260628_142652", 1500)]
    stats = Counter(); mism = []
    for d, mf in demos:
        _run(d, mf, stats, mism)
    print("\n=== native_camera_follow whole-routine shadow (5643 -> 5662) ===")
    for k in sorted(stats):
        print(f"  {k:24s} {stats[k]}")
    for m in mism:
        print("  " + m)
    ok = stats["BAD"] == 0 and stats["ok"] > 0 and not any(k.startswith("ERR") for k in stats)
    print("\nnative_camera_follow:", "PASS (DGROUP byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
