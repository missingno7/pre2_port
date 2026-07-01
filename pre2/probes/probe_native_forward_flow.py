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
                elif ip == 0x5DCC and ns["st"] is not None:            # the VM's idle-fidget read of [0x27F0] (5DCC:
                    ns["idle"] = bytes(mem.data[DS_BASE + 0x27F0:DS_BASE + 0x27F4])   # mov ax,[0x27f0]) — oracle-clock
                elif ip == GAP_SITE and ns["st"] is not None and ns["kbd"] is not None:
                    st = ns["st"]
                    for o, v in ns["kbd"].items():
                        st.data[DS_BASE + o] = v
                    # ORACLE-CLOCK the idle timer: [0x27F0] is bumped by the VM's per-frame PIT-tick count, an
                    # instruction-count quantity the VM-less core can't reproduce. Feed native the exact value the
                    # VM's fidget selector (5DCC) read this frame, so the idle-fidget animation verifies BYTE-EXACT
                    # (the product runtime uses its own faithful native clock; this is verification-only).
                    if ns.get("idle") is not None:
                        st.data[DS_BASE + 0x27F0:DS_BASE + 0x27F4] = ns["idle"]
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
                    # (the idle-fidget anim fields are now BYTE-EXACT via the oracle-clock injection above — the
                    # idle timer [0x27F0] itself stays in _FWD_EXCL, but its downstream fidget pose is verified.)
                    # PC-speaker SFX note state is AUDIO, not gameplay: play_sfx (0282 fall-through) writes the
                    # active-note pointer [0x1035]=0x1037+dl*0xa and the sound engine updates the 11 per-SFX note
                    # structs [0x1037..0x10A5] (0xa bytes each; the digital table [0x1009..0x1035) has 11 entries).
                    # Native emits no sound, so this diverges whenever the VM plays an effect — not a gameplay gap.
                    diffs = [o for o in range(0x10000)
                             if o not in _FWD_EXCL and not _empty_slot_residue(o) and not (0x1035 <= o < 0x10A5)
                             and ((nd[o] ^ vd[o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
                    if diffs:
                        print(f"  FIRST DIVERGENCE at gameplay frame {ns['matched']} (demo f{cur['f']}): {len(diffs)} diffs")
                        for o in diffs[:34]:
                            print(f"     {o:#06x}: n{nd[o]:02x} v{vd[o]:02x}")
                        def _w(d, o): return d[o] | (d[o + 1] << 8)
                        for tag, d in (("NAT", nd), ("VM ", vd)):
                            print(f"     [{tag}] plr=(x{_w(d,0x4F1C):04x} y{_w(d,0x4F1E):04x}) mode[2879]={d[0x2879]:02x} "
                                  f"in[27E8..ED]={d[0x27E8]:02x}{d[0x27E9]:02x}{d[0x27EA]:02x}{d[0x27EB]:02x}{d[0x27EC]:02x}{d[0x27ED]:02x} "
                                  f"src27ED[3f/6e/1f/1a]={d[0x283F]:02x}{d[0x286E]:02x}{d[0x281F]:02x}{d[0x281A]:02x} "
                                  f"6bdb={d[0x6BDB]:02x} a0={d[0x00A0]:02x}{d[0x00A1]:02x}{d[0x00A2]:02x}", flush=True)
                        ns["done"] = True; orig(); return
                    ns["matched"] += 1
                    if ns["matched"] % 1000 == 0:
                        print(f"  ... {ns['matched']} clean frames (demo f{cur['f']}, {ns['respawns']} respawns)", flush=True)
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
