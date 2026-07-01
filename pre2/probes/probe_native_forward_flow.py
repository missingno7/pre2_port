"""FORWARD state verify of the VM-less player on a COLD-START (menu->L1) no-replacements demo.

Unlike probe_native_forward (mid-gameplay snapshot demos, hybrid VM), this replays a pure-ASM demo that navigates
the real menu into a level, auto-detects where gameplay begins (first 021A loop top), seeds a NativeGameState ONCE
there, then runs native_gameplay_frame FORWARD with the demo's input (NO re-seed) and reports the FIRST frame its
DGROUP diverges from the pure-ASM VM. This surfaces MISSING BEHAVIOUR reached through normal play (combat effect
bursts, enemy kills, secret-tile bonuses, ...) that a re-seeded probe silently corrects.

    python -m pre2.probes.probe_native_forward_flow [demo_substr]
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback                       # noqa: E402
from pre2.checkpoints.common import Pre2HybridGap, Pre2RespawnTransition  # noqa: E402
from pre2.native.level_state import native_4f6c                       # noqa: E402
from pre2.native.loop import native_gameplay_frame                    # noqa: E402
from pre2.native.state import NativeGameState                         # noqa: E402
from pre2.probes.probe_native_frame import DECODE, DS_BASE, FRAME_TOP, GAP_SITE, KBD, _SLOT5_PAGE  # noqa: E402
from pre2.probes.probe_native_forward import _FWD_EXCL                # noqa: E402
from pre2.runtime import load_pre2_snapshot                           # noqa: E402
from play import _advance_frame_deterministic, deliver_scancode       # noqa: E402

DS = 0x1A0F
_DEFAULT = "demo_menu_start_L1_20260701_154811"


def _run(demo: str, frame_cap: int = 12000):
    gr = str(ROOT / "assets")
    pb = InputDemoPlayback.load(str(ROOT / "artifacts" / demo))
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 2142)); hz = int(meta.get("present_hz", 70))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=gr, native_replacements=False)   # pure-ASM oracle (the demo's own mode)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    det_speed = max(1, chunk * hz); det = lambda: cpu.instruction_count / det_speed   # noqa: E731
    rt.dos.time_source = det; pic = rt.dos.pic
    args = types.SimpleNamespace(chunk_steps=chunk, steps=None, present_hz=hz, audio="off",
                                 timer_irq=True, input_irq_steps=2_000_000)
    tick = {"next": 0.0}; cur = {"f": 0}
    ns = {"st": None, "kbd": None, "matched": 0, "done": False, "respawns": 0, "seedframe": None}
    orig = cpu.step

    def sstep():
        if not ns["done"]:
            s = cpu.s
            if (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
                ip = s.ip & 0xFFFF
                if ip == FRAME_TOP:                                    # gameplay loop top — only fires in-level
                    if ns["st"] is None:
                        ns["st"] = NativeGameState(bytearray(mem.data)); ns["seedframe"] = cur["f"]
                    ns["kbd"] = None
                elif ip == DECODE and ns["st"] is not None:
                    ns["kbd"] = {o: mem.data[DS_BASE + o] for o in KBD}
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
                        except Exception as e:                        # noqa: BLE001
                            err = "respawn-tail: " + str(e)[:64]
                    except Pre2HybridGap as e:
                        err = "GAP: " + str(e)[:80]
                    except Exception as e:                            # noqa: BLE001
                        err = "ERR " + type(e).__name__ + ": " + str(e)[:64]
                    if err:
                        print(f"  gameplay frame {ns['matched']} (demo f{cur['f']}): could NOT complete -> {err}")
                        ns["done"] = True; orig(); return
                    nd = st.data[DS_BASE:DS_BASE + 0x10000]; vd = mem.data[DS_BASE:DS_BASE + 0x10000]
                    # An EMPTY object/render slot (id [+4]==0xFFFF in the VM) keeps a stale projected X/Y ([+0..3])
                    # that gameplay never reads (every consumer gates on id!=0xFFFF first) — pure render residue from
                    # the transient projection of a since-freed sprite. Skip those X/Y bytes so a benign residue does
                    # not mask the real gameplay frontier (an id divergence is still caught — it's not excluded).
                    def _empty_slot_residue(o):
                        if not (0x4F0A <= o < 0x5732):
                            return False
                        s = 0x4F0A + ((o - 0x4F0A) // 0x12) * 0x12
                        return (o - s) < 4 and vd[s + 4] == 0xFF and vd[s + 5] == 0xFF
                    # The idle-FIDGET animation ([0x4F20] frame / [0x4F28] anim-ptr / [0x4F2C] anim-A state) is
                    # selected from the free-running idle timer [0x27F0] (5DC9 reads [0x27F0]&0x1FF). [0x27F0] is
                    # driven by the VM's per-frame TIMER-tick count (~8/frame in busy L1, variable 4..11) — an
                    # INSTRUCTION-COUNT quantity the VM-less core can't reproduce, so [0x27F0] is already excluded.
                    # When the player is stationary (Xvel [0x4F22]==0, Yvel [0x4F2A]==0) these anim fields are the
                    # pure downstream of that excluded timer, so skip them; the moment the player moves they become
                    # velocity/state-driven again and ARE verified (this gate re-arms every frame).
                    _IDLE_ANIM = {0x4F20, 0x4F21, 0x4F28, 0x4F29, 0x4F2C}
                    idle = (vd[0x4F22] | vd[0x4F23] | vd[0x4F2A] | vd[0x4F2B]) == 0
                    diffs = [o for o in range(0x10000)
                             if o not in _FWD_EXCL and not (idle and o in _IDLE_ANIM) and not _empty_slot_residue(o)
                             and ((nd[o] ^ vd[o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
                    if diffs:
                        print(f"  FIRST DIVERGENCE at gameplay frame {ns['matched']} (demo f{cur['f']}): {len(diffs)} diffs")
                        for o in diffs[:34]:
                            print(f"     {o:#06x}: n{nd[o]:02x} v{vd[o]:02x}")
                        ns["done"] = True; orig(); return
                    ns["matched"] += 1
        orig()

    cpu.step = sstep
    frame = 0
    while not pb.finished(frame) and frame < frame_cap and not ns["done"]:
        cur["f"] = frame
        if not pb.finished(frame):
            pb.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        _advance_frame_deterministic(rt, args, chunk_steps=chunk, sub_batch=2000, clock=det, pic=pic,
                                     sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000,
                                     tick_state=tick, det_speed=det_speed)
        frame += 1
    tag = "to demo end" if not ns["done"] else "DIVERGED"
    print(f"  -> {demo}: gameplay from demo f{ns['seedframe']}; ran FORWARD {ns['matched']} clean gameplay frames "
          f"({ns['respawns']} respawns) [{tag}]")
    return ns["matched"]


def main():
    demo = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT
    print(f"\n{demo}:")
    _run(demo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
