"""Standalone-FORWARD oracle: seed a NativeGameState ONCE, then run native_gameplay_frame FORWARD with the demo's
input (NO re-seed) and find the FIRST frame it diverges from the VM.

This is what the standalone runner actually does, and what the re-seeded probe_native_frame CANNOT see: any
accumulation / collapsed-frame / unrecovered-path bug (the respawn "instant teleport" was the first; "can't kill
enemies", "can't die at 0 lives", missing effects are more of the same). probe_native_frame re-seeds DGROUP from
the VM every frame, so a single wrong frame is silently corrected; here native carries its own state forward, so
the first real divergence surfaces. The demo's input is injected each frame (captured at DC1) so this isolates the
gameplay LOGIC from input plumbing; render/timing/demo-RLE state is excluded.

    python -m pre2.probes.probe_native_forward [demo_substr]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback                       # noqa: E402
from dos_re.interrupts import deliver_scancode                        # noqa: E402
from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition  # noqa: E402
from pre2.native.level_state import native_4f6c                       # noqa: E402
from pre2.native.loop import native_cave_teleport, native_gameplay_frame  # noqa: E402
from pre2.native.state import NativeGameState                         # noqa: E402
from pre2.probes.probe_native_frame import (                          # noqa: E402
    DECODE, DS_BASE, FRAME_TOP, GAP_SITE, KBD, KEY_SAMPLE, _EXCL, _SLOT5_PAGE)
from pre2.runtime import load_pre2_snapshot                           # noqa: E402
import play                                                           # noqa: E402

# The forward-oracle ownership additions (_DEMO_RLE.._PROJ_PTR + _FWD_EXCL) moved VERBATIM to
# pre2/native/seams.py (game_tick_demo's digest is defined by _FWD_EXCL and native must not import the
# probes — they pull the VM at top level). Re-exported so the probe surface is unchanged.
from pre2.native.seams import (DS, _DEMO_RLE, _SLOT_BASE, _SLOT_STRIDE, _NSLOTS, _RENDER_SLOTS,   # noqa: E402,F401
                               _HUD, _SCROLL_Y, _BLIT_SCRATCH, _PROJ_PTR, _FWD_EXCL)


def _run(demo, lim, pure=False):
    pb = InputDemoPlayback.load(str(ROOT / demo))
    # oracle = PURE ASM (native_replacements=False) or the HYBRID VM. Pure ASM proves native == the original
    # binary; a HYBRID-recorded demo desyncs in pure ASM (the demo-clock rides the instruction-per-tick count).
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=not pure)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    ns = {"st": None, "kbd": None, "idle": 0, "f": 0, "matched": 0, "done": False, "respawns": 0}
    orig = cpu.step

    def sstep():
        s = cpu.s
        if not ns["done"] and (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
            ip = s.ip & 0xFFFF
            if ip == FRAME_TOP:
                if ns["st"] is None:
                    ns["st"] = NativeGameState(bytearray(mem.data))   # seed ONCE, then carry forward
                ns["kbd"] = None
            elif ip in (DECODE, KEY_SAMPLE) and ns["st"] is not None:   # base at 0DC1 (always hit; hybrid DC1 is a
                ns["kbd"] = {o: mem.data[DS_BASE + o] for o in KBD}      # replaced hook reading here), overwritten at
                ns["idle"] = mem.data[DS_BASE + 0x27F0] | (mem.data[DS_BASE + 0x27F1] << 8)   # 0F0A (pure ASM, post-reads)
            elif ip == GAP_SITE and ns["st"] is not None and ns["kbd"] is not None:
                st = ns["st"]
                st.data[DS_BASE + 0x27F0] = ns["idle"] & 0xFF          # inject [0x27F0] (not VM-less-reproducible)
                st.data[DS_BASE + 0x27F1] = (ns["idle"] >> 8) & 0xFF   # so the idle-fidget picks the VM's pose
                for o, v in ns["kbd"].items():
                    st.data[DS_BASE + o] = v
                err = None
                try:
                    native_gameplay_frame(st)
                except Pre2CaveTeleport as tp:
                    try:
                        for _ in native_cave_teleport(st, tp.si):     # drain the transition (state-only)
                            pass
                    except Exception as e:                            # noqa: BLE001
                        err = "cave-teleport: " + str(e)[:64]
                except Pre2RespawnTransition:
                    try:
                        for _ in native_4f6c(st):
                            pass
                        ns["respawns"] += 1
                    except Exception as e:                            # noqa: BLE001
                        err = "respawn-tail: " + str(e)[:64]
                except Pre2HybridGap as e:
                    err = "GAP: " + str(e)[:74]
                except Exception as e:                                # noqa: BLE001
                    err = "ERR " + type(e).__name__ + ": " + str(e)[:60]
                if err:
                    print(f"  frame {ns['f']:4d}: native could NOT complete the frame -> {err}")
                    ns["done"] = True; orig(); return
                nd = st.data[DS_BASE:DS_BASE + 0x10000]; vd = mem.data[DS_BASE:DS_BASE + 0x10000]
                # an EMPTY render slot ([+4]==0xFFFF in BOTH) keeps STALE projected X/Y (fields 0-3) — render
                # residue gameplay never reads (native's projection doesn't refresh a freed slot, so it drifts).
                # Exclude it, like the sibling probe_native_forward_flow. Slot 1 (the player, 0x4F1C) is KEPT.
                stale = set()
                for b in range(_SLOT_BASE, 0x5732, _SLOT_STRIDE):
                    if b != 0x4F1C and (nd[b + 4] | (nd[b + 5] << 8)) == 0xFFFF \
                            and (vd[b + 4] | (vd[b + 5] << 8)) == 0xFFFF:
                        stale |= {b, b + 1, b + 2, b + 3}
                diffs = [o for o in range(0x10000)
                         if o not in _FWD_EXCL and o not in stale
                         and ((nd[o] ^ vd[o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
                if diffs:
                    print(f"  FIRST DIVERGENCE at frame {ns['f']} (ran {ns['matched']} clean): {len(diffs)} diffs")
                    for o in diffs[:28]:
                        print(f"     {o:#06x}: n{nd[o]:02x} v{vd[o]:02x}")
                    # dispatch/respawn diagnostic: the 4C69 selectors + pending-death + the backup source
                    for o, nm in ((0x6BE4, "respawn"), (0x6BE5, "gameover"), (0x6BE6, "levelend"),
                                  (0x2879, "realdeath"), (0x920E, "backup[0x8169-src]"), (0x8169, "working")):
                        print(f"     DIAG {o:#06x} {nm:20s}: n{nd[o]:02x} v{vd[o]:02x}")
                    lo89 = [o for o in diffs if 0x815E <= o < 0x9203]
                    hi92 = [o for o in diffs if 0x9203 <= o < 0xA2A8]
                    print(f"     DIAG diffs in working[0x815E..0x9203]={len(lo89)}  backup[0x9203..0xA2A8]={len(hi92)}")
                    ns["done"] = True; orig(); return
                ns["matched"] += 1; ns["f"] += 1
        orig()

    cpu.step = sstep
    # Match the game-tick recording's clock EXACTLY (verify_native_tick_demo): the demo's own chunk_steps (default
    # 2142) + present_hz, and the Sound Blaster ENABLED — the SB ISR/DMA is part of the per-frame instruction budget
    # the demo clock runs on, so replaying without it shifts where every input lands (a different trajectory, so a
    # different/false divergence). Audio output is dropped + excluded from the gameplay compare; only its timing counts.
    from dos_re.runtime import enable_sound_blaster
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 2142)); hz = int(meta.get("present_hz", 70))
    det = lambda: cpu.instruction_count / (chunk * hz); rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    sb = enable_sound_blaster(rt); sb.clock = det
    while not pb.finished(frame) and frame < lim and not ns["done"]:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        if sb.pcm_out:
            sb.pcm_out.clear()
        frame += 1
    tag = "OK" if not ns["done"] else "DIVERGED"
    print(f"  -> {demo.split('/')[-1]}: ran FORWARD {ns['matched']} clean frames "
          f"({ns['respawns']} respawns) [{tag}]")
    return ns["matched"], not ns["done"]


def main():
    pure = "--pure" in sys.argv
    argv = [a for a in sys.argv if a != "--pure"]
    want = argv[1] if len(argv) > 1 else "gorilla"
    lim = int(argv[2]) if len(argv) > 2 else 900
    demos = [d for d in [
        "artifacts/demo_pre2_full_gorilla_20260628_203423",
        "artifacts/demo_pre2_20260629_141422",
    ] if want in d or want == "all"]
    if not demos:
        demos = [f"artifacts/{want}"]
    for d in demos:
        print(f"\n{d.split('/')[-1]}: [oracle={'PURE ASM' if pure else 'hybrid'}]")
        _run(d, lim, pure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
