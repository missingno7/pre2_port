"""Verify the native level loader's deterministic DGROUP slice byte-exact vs the ASM (1030:3ed6).

Booting a NativeGameState from game files means reproducing the level loader 3ed6. This probe captures a
faithful witness — it runs the real 3ed6 in the VM (pure ASM, no native hooks) for a chosen level and snapshots
DGROUP before/after — then seeds a NativeGameState from the same pre-state, runs the recovered
``native_level_load_dgroup``, and asserts the deterministic regions match the ASM exactly:

  * the prologue scalars (level/group/digit/height/level-seg/scroll seeds),
  * the 0x100-word tile-index table ([0x25ce]),
  * the level property tables ([0x7e5e..0x8489], the contiguous 3f3c memcpy).

The object-table region ([0x8489+]) is built by the loader's sub-calls (42af/414d/4182/3ead/40bd/41ca), not yet
recovered — its residual mismatch count is reported for scope, not asserted.

Run: python -m pre2.probes.probe_native_level_load
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback
from pre2.runtime import load_pre2_snapshot
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.native.state import NativeGameState, DATA_SEG
from pre2.native.level_load import (native_level_load_dgroup, native_level_load_objects,
                                    native_level_load_planar)

_DS = DATA_SEG << 4
_LEVEL = 5                      # LEVEL6.SQZ — different from the snapshot's level, so the static tables are
                               # freshly written (a stronger test than a same-level reload).
_SENT = 0xDEAD                  # sentinel return address for the synthetic 3ed6 call


def _run_3ed6(rt, level: int) -> bytes:
    """Run the real 1030:3ed6(level) in the VM to its RET; return the post-state memory image.

    Replicate main's immediate pre-call setup [asm 01c4..01cf]: ``mov ax,0xffff`` + clear [0x4f0a] (0x40b words)
    to 0xFFFF, then ``mov al,[0x2d8a]`` — so AH=0xFF entering 3ed6 (3ed6's own [0x4f1c] pool clear stores AX,
    i.e. fills with 0xFFFF, not a stale AH)."""
    s = rt.cpu.s; mem = rt.cpu.mem
    _ds = 0x1A0F << 4
    for p in range(4):                                                        # CLEAN video memory: zero the 4 EGA
        b = EGA_APERTURE + p * EGA_PLANE_STRIDE                               # planes so the planar-cache compare is
        mem.data[b:b + 0x10000] = b"\x00" * 0x10000                          # vs a known background, not over-draw
    mem.data[_ds + 0x4F0A:_ds + 0x4F0A + 0x40B * 2] = b"\xff" * (0x40B * 2)   # main 0x01c4
    s.sp = (s.sp - 2) & 0xFFFF
    base = (s.ss << 4) + s.sp
    mem.data[base] = _SENT & 0xFF; mem.data[base + 1] = _SENT >> 8
    s.cs = 0x1030; s.ip = 0x3ED6; s.ax = 0xFF00 | (level & 0xFF)             # AH=0xFF (from mov ax,0xffff)
    steps = 0
    while not (s.cs == 0x1030 and s.ip == _SENT):
        rt.cpu.step(); steps += 1
        if steps > 8_000_000:
            raise RuntimeError(f"3ed6 did not return (stuck @ {s.ip:#06x})")
    return bytes(mem.data)


def main() -> int:
    pb = InputDemoPlayback.load(str(ROOT / "artifacts/demo_pre2_full_gorilla_20260628_203423"))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    rt.cpu.trace_enabled = False
    pre = bytes(rt.cpu.mem.data)
    post = _run_3ed6(rt, _LEVEL)                              # the ASM oracle

    state = NativeGameState(bytearray(pre))
    native_level_load_dgroup(state, _LEVEL, game_root=str(ROOT / "assets"))
    native_level_load_objects(state)
    native_level_load_planar(state)
    got = state.data

    # planar local pass: every slot whose index code < 0x100 must demux byte-exact into the 4 EGA planes
    codes = [got[_DS + 0x25CE + i * 2] | (got[_DS + 0x25CE + i * 2 + 1] << 8) for i in range(0x100)]
    cache = 0x5E80
    planar_local_ok = all(
        got[EGA_APERTURE + p * EGA_PLANE_STRIDE + cache + slot * 0x20 + o]
        == post[EGA_APERTURE + p * EGA_PLANE_STRIDE + cache + slot * 0x20 + o]
        for slot, code in enumerate(codes) if code < 0x100
        for p in range(4) for o in range(0x20))
    n_local = sum(1 for c in codes if c < 0x100)

    def region_ok(lo, hi):
        return all(got[_DS + o] == post[_DS + o] for o in range(lo, hi))

    # the deterministic + object/effect-table half of the loader, all expected byte-exact:
    scalars = {0x2D8A: "level", 0x2DAA: "group", 0x2D90: "digit", 0x2CF5: "height", 0x2DDA: "seg_lo",
               0x2DBC: "lvl_adv", 0x2A74: "scr74", 0x2A78: "obj_cnt", 0x6BAD: "hdr0", 0x6BAF: "hdr1"}
    checks = {
        "scalars": all(got[_DS + o] == post[_DS + o] for o in scalars),
        "tile-index [0x25ce]": region_ok(0x25CE, 0x27CE),
        "property tables [0x7e5e]": region_ok(0x7E5E, 0x8489),
        "object+effect lists [0x8489]": region_ok(0x8489, 0x9203),
        "double-buffer dup [0x9203]": region_ok(0x9203, 0xA2A8),
        f"planar tile cache (local x{n_local})": planar_local_ok,
    }
    for name, good in checks.items():
        print(f"  {'OK  ' if good else 'FAIL'} {name}")
    # not-yet-recovered halves (reported for scope, not asserted): classify_sprites (4232) -> [0x2df8/0x4df8],
    # the 42af tile tables -> [0x6688], the 3ead/41ca high-memory writes, and the planar tile blit.
    classify = sum(1 for o in range(0x2DF8, 0x3A58) if got[_DS + o] != post[_DS + o])
    tiletab = sum(1 for o in range(0x6688, 0x6A88) if got[_DS + o] != post[_DS + o])
    print(f"  (deferred: classify-sprites region {classify}B, 42af tile-tables {tiletab}B)")
    ok = all(checks.values())
    print("native level-load (DGROUP + object tables):", "PASS (byte-exact vs ASM)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
