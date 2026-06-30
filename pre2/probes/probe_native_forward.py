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
from pre2.checkpoints.common import Pre2HybridGap, Pre2RespawnTransition  # noqa: E402
from pre2.native.level_state import native_4f6c                       # noqa: E402
from pre2.native.loop import native_gameplay_frame                    # noqa: E402
from pre2.native.state import NativeGameState                         # noqa: E402
from pre2.probes.probe_native_frame import (                          # noqa: E402
    DECODE, DS_BASE, FRAME_TOP, GAP_SITE, KBD, _EXCL, _SLOT5_PAGE)
from pre2.runtime import load_pre2_snapshot                           # noqa: E402
import play                                                           # noqa: E402

DS = 0x1A0F
# the demo-RLE playback cursor + scratch (DS:0x3F {byte,count}) is input plumbing the standalone runner never uses
# (it reads live input), so exclude it from the forward gameplay compare.
_DEMO_RLE = set(range(0x3C, 0x60))
# the render slot array (base 0x4F0A, stride 0x12): slot 1 is the player, slots >=2 are render records the ASM
# render maintains (8922 project_particles + 26FA object_render fill [+4] sprite-id / [+5] page / [+0xc] and FREE
# [+4]=0xFFFF on expiry). native skips those render routines, so these slots go stale in the forward run — this
# is the SEPARATE native-render-state gap (the missing effect sprites, e.g. the spider's web). Excluded here so
# the GAMEPLAY-logic forward verify can run past it (slot 1 = player and all entity DATA stay fully checked).
_SLOT_BASE, _SLOT_STRIDE = 0x4F0A, 0x12
_NSLOTS = max((p - (_SLOT_BASE + 5)) // _SLOT_STRIDE for p in _SLOT5_PAGE) + 1
_RENDER_SLOTS = {_SLOT_BASE + k * _SLOT_STRIDE + f for k in range(2, _NSLOTS) for f in (4, 5, 0xC, 0xD)}
_FWD_EXCL = set(_EXCL) | _DEMO_RLE | _RENDER_SLOTS


def _run(demo, lim):
    pb = InputDemoPlayback.load(str(ROOT / demo))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    ns = {"st": None, "kbd": None, "f": 0, "matched": 0, "done": False, "respawns": 0}
    orig = cpu.step

    def sstep():
        s = cpu.s
        if not ns["done"] and (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
            ip = s.ip & 0xFFFF
            if ip == FRAME_TOP:
                if ns["st"] is None:
                    ns["st"] = NativeGameState(bytearray(mem.data))   # seed ONCE, then carry forward
                ns["kbd"] = None
            elif ip == DECODE and ns["st"] is not None:
                ns["kbd"] = {o: mem.data[DS_BASE + o] for o in KBD}    # inject the demo's input this frame
            elif ip == GAP_SITE and ns["st"] is not None and ns["kbd"] is not None:
                st = ns["st"]
                for o, v in ns["kbd"].items():
                    st.data[DS_BASE + o] = v
                err = None
                try:
                    native_gameplay_frame(st)
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
                diffs = [o for o in range(0x10000)
                         if o not in _FWD_EXCL and ((nd[o] ^ vd[o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
                if diffs:
                    print(f"  FIRST DIVERGENCE at frame {ns['f']} (ran {ns['matched']} clean): {len(diffs)} diffs")
                    for o in diffs[:28]:
                        print(f"     {o:#06x}: n{nd[o]:02x} v{vd[o]:02x}")
                    ns["done"] = True; orig(); return
                ns["matched"] += 1; ns["f"] += 1
        orig()

    cpu.step = sstep
    det = lambda: cpu.instruction_count / (2142 * 70); rt.dos.time_source = det; tick = {"next": 0.0}; frame = 0
    while not pb.finished(frame) and frame < lim and not ns["done"]:
        pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        frame += 1
    tag = "OK" if not ns["done"] else "DIVERGED"
    print(f"  -> {demo.split('/')[-1]}: ran FORWARD {ns['matched']} clean frames "
          f"({ns['respawns']} respawns) [{tag}]")
    return ns["matched"], not ns["done"]


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else "gorilla"
    demos = [d for d in [
        "artifacts/demo_pre2_full_gorilla_20260628_203423",
        "artifacts/demo_pre2_20260629_141422",
    ] if want in d or want == "all"]
    if not demos:
        demos = [f"artifacts/{want}"]
    for d in demos:
        print(f"\n{d.split('/')[-1]}:")
        _run(d, 900)
    return 0


if __name__ == "__main__":
    sys.exit(main())
