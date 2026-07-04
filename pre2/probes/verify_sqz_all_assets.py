"""Exhaustive SQZ codec proof: ASM 107B output == pre2.codecs.sqz.unpack_sqz for EVERY .SQZ asset.

The decode hooks (sqz_decompress) cannot be justified for the --safe-hooks oracle by write-set ownership —
their output (level maps, sprite banks) IS gameplay-read. Their justification is different: the input domain
is CLOSED (the finite set of .SQZ files in the game root) and the decode is a pure function of the file, so
recovered==ASM can be proven over the ENTIRE real input domain, once, offline — this probe is that proof.

Per asset: write the ASCIIZ filename into the DGROUP filename scratch (DS:0x22 — the exact slot the game's
own callers point DX at, see the 011C caller), reset the bump allocator [0x2875] to the snapshot's value,
synthetically CALL the original ASM loader+decompressor 1030:107B (0xDEAD sentinel return), and memcmp the
ASM's decoded bytes at [0x2875]:0 against unpack_sqz(file). Any mismatch prints the first diverging offset.

    python pre2/probes/verify_sqz_all_assets.py [snapshot_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from pre2.codecs.sqz import unpack_sqz          # noqa: E402
from pre2.runtime import load_pre2_snapshot     # noqa: E402

DS = 0x1A0F
NAME_SCRATCH = 0x22          # DS:0x22 — the filename slot the game's own load callers use (mov dx,0x22)
LOAD_PTR = 0x2875            # the bump allocator (output segment)
ENTRY = 0x107B               # load + decompress (dx = ASCIIZ filename in DS)
STEP_CAP = 60_000_000        # big LZSS assets take tens of millions of interpreted steps


def _call_asm_decode(rt, name: str) -> tuple[int, int]:
    """Synthetically call 107B for ``name``; returns (out_seg, steps). The sentinel-return pattern of
    pre2/probes/probe_native_level_init._invoke_asm."""
    cpu = rt.cpu
    mem = cpu.mem
    s = cpu.s
    base = (DS << 4) + NAME_SCRATCH
    blob = name.encode("ascii") + b"\x00"
    mem.data[base:base + len(blob)] = blob
    s.ds = DS
    s.dx = NAME_SCRATCH
    out_seg = mem.data[(DS << 4) + LOAD_PTR] | (mem.data[(DS << 4) + LOAD_PTR + 1] << 8)
    ss = s.ss & 0xFFFF
    s.cs = 0x1030
    s.ip = ENTRY
    s.sp = (s.sp - 2) & 0xFFFF
    mem.data[(ss << 4) + s.sp] = 0xAD
    mem.data[(ss << 4) + ((s.sp + 1) & 0xFFFF)] = 0xDE
    n = 0
    while n < STEP_CAP:
        if (s.ip & 0xFFFF) == 0xDEAD and (s.cs & 0xFFFF) == 0x1030:
            break
        cpu.step()
        n += 1
    else:
        raise RuntimeError(f"{name}: ASM decode did not return within {STEP_CAP} steps")
    return out_seg, n


def main() -> int:
    snap = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260704_213408/snapshot"
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), str(ROOT / snap),
                            game_root=str(ROOT / "assets"), native_replacements=False)   # pure ASM
    cpu = rt.cpu
    cpu.trace_enabled = False
    boot_load_seg = cpu.mem.data[(DS << 4) + LOAD_PTR] | (cpu.mem.data[(DS << 4) + LOAD_PTR + 1] << 8)

    assets = sorted(p for p in (ROOT / "assets").iterdir() if p.suffix.upper() == ".SQZ")
    print(f"sweeping {len(assets)} .SQZ assets: ASM 1030:107B vs pre2.codecs.sqz.unpack_sqz "
          f"(scratch seg {boot_load_seg:#06x})")
    fails = []
    for p in assets:
        expected = unpack_sqz(p.read_bytes())
        # reset the bump allocator so every decode lands on the same scratch (no cumulative overflow)
        cpu.mem.data[(DS << 4) + LOAD_PTR] = boot_load_seg & 0xFF
        cpu.mem.data[(DS << 4) + LOAD_PTR + 1] = (boot_load_seg >> 8) & 0xFF
        try:
            out_seg, steps = _call_asm_decode(rt, p.name.upper())
        except RuntimeError as e:
            print(f"  FAIL  {p.name:14s} {e}")
            fails.append(p.name)
            continue
        base = (out_seg << 4) & 0xFFFFF
        asm_out = bytes(cpu.mem.data[base:base + len(expected)])
        if asm_out == expected:
            print(f"  OK    {p.name:14s} {len(expected):7d} bytes  ({steps:,} steps)")
        else:
            i = next(k for k in range(len(expected)) if asm_out[k] != expected[k])
            print(f"  FAIL  {p.name:14s} first diff @{i}/{len(expected)}: asm={asm_out[i]:02X} "
                  f"rec={expected[i]:02X}")
            fails.append(p.name)
    print(f"\n{len(assets) - len(fails)}/{len(assets)} assets byte-identical"
          + (f"; FAILURES: {fails}" if fails else " — the recovered codec equals the ASM over the "
                                                  "ENTIRE asset set (the closed input domain)"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
