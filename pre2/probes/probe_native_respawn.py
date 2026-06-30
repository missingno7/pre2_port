"""Timer-driven byte-exact verify of the blocking respawn handler native_4f6c vs the ASM.

native_4f6c blocks across many frames (the 509d death-bounce + 3af2's camera re-scroll, whose VGA-retrace +
timer-ISR busy-waits need the full timing machinery), so a raw synthetic invoke hangs. Drive the ASM 4F6C through
``play._advance_demo_frame`` (timer IRQ + retrace) with a jmp-self sentinel at the return so the VM parks safely,
then diff native vs the ASM's DGROUP at the RET 0x5033 (render/timing state excluded — the renderer's job).

    python -m pre2.probes.probe_native_respawn
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


def _verify(name, entry, ret_ip, native_fn, totals, cap=3000):
    pb = InputDemoPlayback.load(str(ROOT / _SNAP))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem; s = cpu.s
    rt.dos.vga_retrace_active_fraction = 0.3                    # let the render busy-waits complete
    det = lambda: cpu.instruction_count / (2142 * 70); rt.dos.time_source = det; tick = {"next": 0.0}
    ss = s.ss & 0xFFFF; sp0 = s.sp & 0xFFFF
    mem.data[CS + 0xDEAD] = 0xEB; mem.data[CS + 0xDEAE] = 0xFE  # jmp-self landing pad for the sentinel return
    pre = bytearray(mem.data)                                  # AFTER the patch (CS:0xDEAD aliases DGROUP 0x40bd)
    s.cs = 0x1030; s.ip = entry; s.sp = (sp0 - 2) & 0xFFFF
    mem.data[(ss << 4) + s.sp] = 0xAD; mem.data[(ss << 4) + ((s.sp + 1) & 0xFFFF)] = 0xDE
    cap_d = {"post": None}; orig = cpu.step

    def sstep():
        if cap_d["post"] is None and (cpu.s.cs & 0xFFFF) == 0x1030 and (cpu.s.ip & 0xFFFF) == ret_ip:
            cap_d["post"] = bytes(mem.data)
        orig()

    cpu.step = sstep
    i = 0
    for i in range(cap):
        if cap_d["post"] is not None:
            break
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
    if cap_d["post"] is None:
        print(f"  {name}: FAIL (RET {ret_ip:#06x} not reached in {i} chunks)"); totals["fail"] += 1; return
    post = cap_d["post"]
    st = NativeGameState(bytearray(pre))
    for _ in native_fn(st):                                         # native_4f6c is now a per-frame generator;
        pass                                                       #   drive all 60 bounce frames + the restore
    nd = st.data
    excl = set(_EXCL) | {0x2DEC, 0x2DED}
    diffs = [o for o in range(0x10000)
             if o not in excl and ((nd[DS + o] ^ post[DS + o]) & (0x9F if o in _SLOT5_PAGE else 0xFF))]
    print(f"  {name:18s} (ASM {entry:#06x}->{ret_ip:#06x}, {i} chunks): {len(diffs)} DGROUP diffs")
    for o in diffs[:12]:
        print(f"     {o:#06x}: n{nd[DS + o]:02x} v{post[DS + o]:02x}")
    if diffs:
        totals["fail"] += 1


def main():
    totals = {"fail": 0}
    _verify("native_4f6c", 0x4F6C, 0x5033, native_4f6c, totals)
    ok = totals["fail"] == 0
    print("\nrespawn handler vs ASM:", "PASS (byte-exact, render/timing excluded)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
