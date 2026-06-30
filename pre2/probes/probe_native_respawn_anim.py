"""Per-FRAME byte-exact verify of the death-bounce ANIMATION: native_4f6c (a per-frame generator) vs the ASM
509d loop, frame by frame — not just at the endpoint.

This closes the verification hole that let "instant respawn" through: the whole-loop verify (probe_native_frame)
re-seeds DGROUP from the VM every frame, so it never actually ran the standalone runner's MULTI-FRAME respawn
sequence — it only ever checked one frame at a time. The endpoint verify (probe_native_respawn) proved the
respawn's FINAL state matches, but the runner renders the 60 bounce frames one at a time, so what the player sees
is the per-frame arc. This proves each of those 60 frames is byte-exact vs the ASM.

Method (same synthetic-invoke harness as probe_native_respawn): drive the ASM 4F6C with a jmp-self sentinel
return through the timer/retrace machinery, capturing DGROUP at each 509d loop-top (0x50df, fires once per bounce
frame); drive the native_4f6c generator over the same pre-state, capturing DGROUP at each yield; diff them frame
by frame (render/timing/audio-ISR excluded — the renderer's + the emulated timer's job, not the bounce physics).

    python -m pre2.probes.probe_native_respawn_anim
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback                 # noqa: E402
from pre2.native.level_state import native_4f6c                 # noqa: E402
from pre2.native.state import NativeGameState                   # noqa: E402
from pre2.probes.probe_native_frame import _EXCL, _SLOT5_PAGE   # noqa: E402
from pre2.runtime import load_pre2_snapshot                     # noqa: E402
import play                                                     # noqa: E402

DS = 0x1A0F << 4
CS = 0x1030 << 4
_SNAP = "artifacts/demo_pre2_full_gorilla_20260628_203423"
_LOOP_TOP = 0x50DF       # the 509d per-frame loop body start (mov word [0x4f0e],0xffff) — once per bounce frame
_RET = 0x5033            # 4F6C return
# the emulated PIT/PIC ISR runs on the VM side (driving the render busy-waits) but not on the native side, so its
# audio-mixer + timer scratch is excluded alongside the render state (it is not the bounce physics).
_AUDIO_ISR = {0x27F0, 0x27F1} | set(range(0x2820, 0x2880))


def main():
    pb = InputDemoPlayback.load(str(ROOT / _SNAP))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem; s = cpu.s
    rt.dos.vga_retrace_active_fraction = 0.3                    # let the render busy-waits complete
    det = lambda: cpu.instruction_count / (2142 * 70); rt.dos.time_source = det; tick = {"next": 0.0}
    ss = s.ss & 0xFFFF; sp0 = s.sp & 0xFFFF
    mem.data[CS + 0xDEAD] = 0xEB; mem.data[CS + 0xDEAE] = 0xFE  # jmp-self landing pad for the sentinel return
    pre = bytearray(mem.data)                                  # AFTER the patch (CS:0xDEAD aliases DGROUP 0x40bd)
    s.cs = 0x1030; s.ip = 0x4F6C; s.sp = (sp0 - 2) & 0xFFFF
    mem.data[(ss << 4) + s.sp] = 0xAD; mem.data[(ss << 4) + ((s.sp + 1) & 0xFFFF)] = 0xDE

    vm_frames: list[bytes] = []; done = {"ret": False}; orig = cpu.step

    def sstep():
        cs = cpu.s.cs & 0xFFFF; ip = cpu.s.ip & 0xFFFF
        if cs == 0x1030 and ip == _LOOP_TOP:
            vm_frames.append(bytes(mem.data))                  # state at the loop top, BEFORE this frame's work
        if cs == 0x1030 and ip == _RET:
            done["ret"] = True
        orig()

    cpu.step = sstep
    for _ in range(3000):
        if done["ret"]:
            break
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)

    # native: drive the SAME per-frame generator over the same pre-state, capturing DGROUP at each yield
    st = NativeGameState(bytearray(pre)); native_frames: list[bytes] = []
    for _ in native_4f6c(st):
        native_frames.append(bytes(st.data))

    # The render DRAW-LIST: the slot array (base 0x4F0A, stride 0x12) — slot 1 (0x4F1C) is the PLAYER struct, but
    # the higher slots are the renderer's compacted sprite-output list. The ASM render cluster (3a27/26fa, which
    # native skips) fills slot+4/+5 (sprite source + page) and decrements slot+0xc (a lifetime) as effects expire;
    # native_render REBUILDS this list from game state every frame, so a stale slot never reaches the screen.
    # Excluded ONLY for slots >= 2 — the player (slot 1) and all true gameplay fields stay fully checked, and any
    # diff OUTSIDE this set is reported loudly (so the exclusion can never silently hide a real divergence).
    _SLOT_BASE, _SLOT_STRIDE = 0x4F0A, 0x12
    _nslots = max((p - (_SLOT_BASE + 5)) // _SLOT_STRIDE for p in _SLOT5_PAGE) + 1
    _render_drawlist = {_SLOT_BASE + k * _SLOT_STRIDE + f
                        for k in range(2, _nslots) for f in (4, 5, 0xC, 0xD)}
    excl = set(_EXCL) | _AUDIO_ISR | _render_drawlist | {0x2DEC, 0x2DED}

    print(f"VM 509d loop-top frames: {len(vm_frames)}   native bounce yields: {len(native_frames)}")
    n = min(len(vm_frames), len(native_frames))
    frames_failed = 0
    for k in range(n):
        vd, nd = vm_frames[k], native_frames[k]
        diffs = [o for o in range(0x10000)
                 if o not in excl and ((nd[DS + o] ^ vd[DS + o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
        if diffs:
            frames_failed += 1
            if frames_failed <= 6:                                 # any diff here is OUTSIDE the render draw-list
                print(f"  frame {k:2d}: {len(diffs)} non-render diffs  " +
                      " ".join(f"{o:#06x}:n{nd[DS + o]:02x}/v{vd[DS + o]:02x}" for o in diffs[:8]))
    ok = frames_failed == 0 and len(vm_frames) == len(native_frames) == 0x3C
    print(f"\nframes compared: {n}   frames with non-render diffs: {frames_failed}")
    print("death-bounce per-frame vs ASM:",
          "PASS (all 60 bounce frames byte-exact; only the render draw-list — which native_render rebuilds — differs)"
          if ok else "FAIL (real per-frame divergence outside the render draw-list)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
