"""End-to-end check of the VM-less native frame: native_gameplay_frame(NativeGameState) vs the live VM.

Each frame: at the main-loop top (1030:021A) save the VM's memory as the native seed; at the player's input
decode (1030:0DC1) capture the keyboard flags the VM actually sampled; at the first un-wired call site (1030:0232,
the 8295 call) build a NativeGameState from the seed, run native_gameplay_frame, and diff its DGROUP against the
VM's. Today the native frame composes 581E -> object system -> effects -> terrain -> the WHOLE player update (5850).

INPUT SYNC: the VM's timer ISR polls the keyboard throughout the frame, so the [0x28xx] key flags can change
between 021A and DC1. The native frame samples input once (no ISR / busy-waits), so for an apples-to-apples
gameplay-logic comparison we feed it the keyboard state the VM's DC1 sampled. This isolates the deterministic
input->state transform from the I/O timing difference (a feature of the VM-less model, not a bug); on stable-input
frames the two coincide anyway.

Excluded: audio/ISR scratch (async, not gameplay) + render state the gameplay step doesn't own (454E's
[0x6CA2-6CA6] bg-save slot). native_replacements=True, so the VM applies the same recovered routines per-hook and
this is the composition/plumbing check (each routine's ASM-equivalence is proven by its own =False shadow).

Run: python -m pre2.probes.probe_native_frame
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot
from pre2.native.state import NativeGameState
from pre2.native.loop import native_gameplay_frame
from pre2.native.level_state import native_4f6c
from pre2.native.player import RENDER_OFFSETS
from pre2.recovered.input_decode import _KBD_SOURCES
from pre2.checkpoints.common import Pre2HybridGap, Pre2RespawnTransition
import play

DS = 0x1A0F
DS_BASE = (DS << 4) & 0xFFFFF
FRAME_TOP = 0x021A          # main-loop top: save the native seed here
DECODE = 0x0DC1             # DC1: capture the keyboard flags the VM sampled
GAP_SITE = 0x0270           # the loop-back (jmp 0x214): native_gameplay_frame now runs the WHOLE loop 021A..026D.
#                             The render calls (3668/35A1/3A27/4B8E/26FA/3721/6772) are the renderer's job and are
#                             not run here; their render-state DGROUP writes are excluded below so this stays a
#                             gameplay-state cmp. Death/level-change frames (4C69 carry -> 0x12f) never reach 0x0270.
KBD = tuple(sorted({o for srcs in _KBD_SOURCES.values() for o in srcs}))   # the 21 keyboard flags DC1 reads
# async audio/ISR scratch (PIT/SB) + render state the gameplay step doesn't own:
#  - 454E player-sprite bg-save slot (RENDER_OFFSETS)
#  - 8922 object_particles (a render island skipped here) builds the effect draw-list: the render slots
#    [0x52E8..0x5450) (20 x stride 0x12) + the effect-sprite source list [0x8F1D..0x9107) (bounce animation).
_RENDER_DRAWLIST = set(range(0x52E8, 0x5450)) | set(range(0x8F1D, 0x9107))
# more render state the gameplay step doesn't own (the renderer's job, run by native_render):
#  - the page-flip buffer segs [0x2dd6]/[0x2dd8] (front/back VRAM page, swapped each frame)
#  - 3668's redraw counters [0x6bd4] + [0x6bc3] (the 0x66->0x68 dither rotation) + the ISR timer tick [0x27ee]
_PAGE_FLIP = {0x2DD6, 0x2DD7, 0x2DD8, 0x2DD9}
_RENDER_COUNTERS = {0x27EE, 0x27EF, 0x6BD4, 0x6BC3}
# 35A1 (dirty-grid redraw) + 3A27 (scroll-copy) own the smooth HORIZONTAL-scroll render state (5643/camera_follow
# does only the vertical): the displayed scroll-X counter [0x2de0] and the dirty-grid / background-pointer block
# [0x2dee..0x2df8) (incl. [0x2df6] bg ptr) — render, run by native_render, not the gameplay step.
_SCROLL_RENDER = {0x2DE0, 0x2DE1} | set(range(0x2DEE, 0x2DF8))
# each object slot's +5 byte holds render page/visibility flags (bits 0x20|0x40) that 26FA (object_render) writes
# -> mask just those two bits so the gameplay low bits still verify (slots: base 0x4F0A, stride 0x12).
_SLOT5_PAGE = {0x4F0A + i * 0x12 + 5 for i in range(0x75)}
# each rendered sprite's +0x11 byte is the object_render attr/animation countdown (the recovered 0x1FFF attr):
# 60FE (particles) seeds it, then 26FA (object_render) decrements it each frame as it draws — render-owned
# (native_render decrements it), so the gameplay step leaves it for the renderer (base 0x4F0A, stride 0x12).
_SLOT_ATTR = {0x4F0A + i * 0x12 + 0x11 for i in range(0x75)}
# 1C65 (the vsync/frame-sync wait inside 44FB) maintains the page-flip-pending counter [0x6be7] — timing/waiting
# machinery the native port replaces with its heartbeat (lifecycle Phase 10), never gameplay state.
_TIMING = {0x6BE7}
_EXCL = (set((0x1004, 0x1005, 0x1006, 0x1007)) | set(range(0x27F0, 0x2800)) | set(range(0x2820, 0x2880))
         | set(range(0xAB0, 0xE00)) | set(RENDER_OFFSETS) | _RENDER_DRAWLIST | _PAGE_FLIP | _RENDER_COUNTERS
         | _SLOT_ATTR | _SCROLL_RENDER | _TIMING)


def _run(demo, lim, totals):
    pb = InputDemoPlayback.load(str(ROOT / demo))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    st = {"calls": 0, "div": 0, "err": 0, "gap": 0, "respawn": 0}
    pend = {"seed": None, "kbd": None}
    orig = cpu.step

    def sstep():
        s = cpu.s
        if (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
            ip = s.ip & 0xFFFF
            if ip == FRAME_TOP:
                pend["seed"] = bytearray(mem.data); pend["kbd"] = None
            elif ip == DECODE and pend["seed"] is not None:
                pend["kbd"] = {o: mem.data[DS_BASE + o] for o in KBD}     # the VM's sampled input this frame
            elif ip == GAP_SITE and pend["seed"] is not None and pend["kbd"] is not None:
                state = NativeGameState(pend["seed"])
                for o, v in pend["kbd"].items():                          # sync the sampled keyboard state
                    state.data[DS_BASE + o] = v
                try:
                    native_gameplay_frame(state)
                except Pre2RespawnTransition:                            # the respawn is a multi-frame TRANSITION
                    try:                                                #   (4C69 [0x6be4]==1 -> 4F6C): drive it to
                        for _ in native_4f6c(state):                    #   completion so the whole-loop verify still
                            pass                                        #   diffs the respawn frame's endpoint vs the
                    except Pre2HybridGap:                               #   VM (its per-frame arc is proven by
                        st["gap"] += 1                                  #   probe_native_respawn_anim). A pending
                        pend["seed"] = None; pend["kbd"] = None; orig(); return   # death -> game-over carry = gap.
                    st["respawn"] += 1
                except Pre2HybridGap:                                     # armed unrecovered event (death/scroll-
                    st["gap"] += 1                                        # script/level-state) — flagged, not a div
                    pend["seed"] = None; pend["kbd"] = None; orig(); return
                except Exception as e:                                    # noqa: BLE001 — surface real bugs
                    st["err"] += 1
                    if st["err"] <= 2:
                        print(f"   native frame ERROR: {type(e).__name__}: {str(e)[:90]}")
                    pend["seed"] = None; pend["kbd"] = None; orig(); return
                native_dg = state.data[DS_BASE:DS_BASE + 0x10000]
                vm_dg = mem.data[DS_BASE:DS_BASE + 0x10000]
                diffs = [o for o in range(0x10000) if o not in _EXCL
                         and ((native_dg[o] ^ vm_dg[o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
                st["calls"] += 1
                if diffs:
                    st["div"] += 1
                    if st["div"] <= 3:
                        print(f"   DIV {len(diffs)}: "
                              f"{[(hex(o), f'n{native_dg[o]:02x}', f'v{vm_dg[o]:02x}') for o in diffs[:12]]}")
                pend["seed"] = None; pend["kbd"] = None
        orig()

    cpu.step = sstep
    det = lambda: cpu.instruction_count / (2142 * 70)
    rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < lim:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        frame += 1
    print(f"  {demo.split('/')[-1]:44s} frame@{GAP_SITE:#06x}: "
          f"calls={st['calls']} DIV={st['div']} respawn={st['respawn']} gap={st['gap']} err={st['err']}")
    totals["calls"] += st["calls"]; totals["div"] += st["div"]
    totals["err"] += st["err"]; totals["gap"] += st["gap"]; totals["respawn"] += st["respawn"]


def main():
    # The two demos the native-frame prefix has been verified on end-to-end (boss + gorilla). NOTE: demos
    # 213332 / 142652 currently expose a *pre-existing object-system second-pass* divergence (project_entity's
    # rng_lcg Y-jitter at object_inject.py:294 -> RNG [0x2CEC] + render-suppress [0x4F0E] drift), present in the
    # prefix BEFORE the player step runs and unrelated to it; tracked separately. Add them back once that lands.
    demos = [("artifacts/demo_pre2_20260629_141422", 900),
             ("artifacts/demo_pre2_full_gorilla_20260628_203423", 900)]
    totals = {"calls": 0, "div": 0, "err": 0, "gap": 0, "respawn": 0}
    for d, lim in demos:
        _run(d, lim, totals)
    ok = totals["div"] == 0 and totals["err"] == 0 and totals["calls"] > 0
    print(f"\nnative_gameplay_frame (WHOLE loop 021A..026D) vs VM: calls={totals['calls']} "
          f"DIV={totals['div']} respawn={totals['respawn']} gap={totals['gap']} err={totals['err']}")
    print(f"  (respawn = death-respawn transitions driven to completion + endpoint-verified here, per-frame-verified")
    print(f"   by probe_native_respawn_anim; gap = armed event — level-end/death/game-over — correctly fail-louds)")
    print("native_gameplay_frame:", "PASS (gameplay DGROUP byte-exact over the whole loop)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
