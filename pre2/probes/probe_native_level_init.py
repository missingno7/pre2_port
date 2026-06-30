"""Synthetic shadow-verify the native level-init leaves (pre2/native/level_init.py) against the ASM at their CS:IP.

Each leaf is invoked on the ASM side by hijacking the CPU over a snapshot — push a 0xDEAD sentinel return, set
cs:ip to the routine, step to the RET — then the native function is run over the same pre-state and the DGROUP is
diffed. The render sub-calls these routines make (palette int 10h, sprite blits into [0x2dda]) touch no DGROUP, so
the gameplay-state diff is clean.

    python -m pre2.probes.probe_native_level_init
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback            # noqa: E402
from pre2.native.level_init import native_3af2, native_5237  # noqa: E402
from pre2.native.state import NativeGameState              # noqa: E402
from pre2.runtime import load_pre2_snapshot                # noqa: E402

DS = 0x1A0F << 4
_SNAP = "artifacts/demo_pre2_full_gorilla_20260628_203423"
# 3af2's screen-draw + scroll-copy leave render-pointer state in DGROUP (the renderer's job, not gameplay):
_RENDER_3AF2 = {0x2DBA, 0x2DBB, 0x2DE8, 0x2DE9, 0x2DF2, 0x2DF3, 0x2DF5, 0x2DF6, 0x2DF7}


def _invoke_asm(rt, entry):
    """Synthetically call the ASM routine at ``entry``: push a 0xDEAD sentinel return, set cs:ip, step to the RET."""
    cpu = rt.cpu; mem = cpu.mem; s = cpu.s
    ss = s.ss & 0xFFFF; sp_init = s.sp & 0xFFFF
    s.cs = 0x1030; s.ip = entry
    s.sp = (sp_init - 2) & 0xFFFF
    mem.data[(ss << 4) + s.sp] = 0xAD
    mem.data[(ss << 4) + ((s.sp + 1) & 0xFFFF)] = 0xDE
    n = 0
    while n < 2_000_000:
        if (s.ip & 0xFFFF) == 0xDEAD and (s.cs & 0xFFFF) == 0x1030:
            break
        cpu.step(); n += 1
    return n, ss, sp_init


def _verify(name, entry, native_fn, totals, extra_excl=frozenset()):
    pb = InputDemoPlayback.load(str(ROOT / _SNAP))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)   # pure ASM oracle
    rt.cpu.trace_enabled = False
    pre = bytearray(rt.cpu.mem.data)
    st = NativeGameState(bytearray(pre)); native_fn(st)
    native_dg = bytes(st.data[DS:DS + 0x10000])
    n, ss, sp = _invoke_asm(rt, entry)
    vm_dg = rt.cpu.mem.data[DS:DS + 0x10000]
    # exclude async ISR scratch (PIT/SB) + the stack region if SS aliases DGROUP
    excl = set(range(0x27EE, 0x2800)) | set(range(0x2820, 0x2880)) | {0x1004, 0x1005, 0x1006, 0x1007}
    excl |= set(extra_excl)
    if ss == 0x1A0F:
        excl |= set(range((sp - 0x300) & 0xFFFF, 0x10000))
    diffs = [o for o in range(0x10000) if native_dg[o] != vm_dg[o] and o not in excl]
    print(f"  {name:18s} (ASM {entry:#06x}, {n} steps): {len(diffs)} DGROUP diffs")
    for o in diffs[:12]:
        print(f"     {o:#06x}: n{native_dg[o]:02x} v{vm_dg[o]:02x}")
    totals["fail"] += 0 if not diffs else 1


def main():
    totals = {"fail": 0}
    _verify("native_5237", 0x5237, native_5237, totals)
    _verify("native_3af2", 0x3AF2, native_3af2, totals, extra_excl=_RENDER_3AF2)
    ok = totals["fail"] == 0
    print("\nnative level-init leaves vs ASM:", "PASS (byte-exact)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
