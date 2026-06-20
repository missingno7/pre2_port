"""TEMPORARY probe — capture the load-time sprite-decode witness.

The mid-gameplay snapshot is not a faithful witness for the sprite decode/classify
island (source asset RAM freed, VRAM cache over-drawn). This probe drives the
menu->level-load demo and transparently observes the original ASM at the decode/
classify boundaries, dumping {asset bytes, resulting 4-plane VRAM cache, type
table} so the recovered transform can be verified byte-exact against the ASM.

Boundaries (seg 1030, see docs/pre2/symbol_ledger.md):
  42F7 local sprite decode (entry)   / 4369 ret
  436A shared sprite decode (entry)  / 43B2 ret
  4213 classifier (entry)            / 428F ret

Run:  python -m pre2.probes.capture_sprite_decode
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.runtime import load_pre2_snapshot

SEG = 0x1030
DS = 0x1A13
CACHE_OFF = 0x5E80
CACHE_BYTES = 256 * 0x20  # 256 slots x 32 bytes per plane
DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
OUT = ROOT / "artifacts" / "sprite_decode_witness"


def _rw(mem, seg, off):
    a = (seg << 4) + off
    return mem.data[a] | (mem.data[a + 1] << 8)


def _rb(mem, seg, off):
    return mem.data[(seg << 4) + off]


def _src_seg_local(mem):
    # 42F7: src = [0x2DD6] + ([ [0x2D86] + 0x2D2C ] << 4)
    dd6 = _rw(mem, DS, 0x2DD6)
    d86 = _rb(mem, DS, 0x2D86)
    mult = _rb(mem, DS, 0x2D2C + d86)
    return (dd6 + (mult << 4)) & 0xFFFF, dd6, d86, mult


def _cache_planes(mem):
    planes = []
    for p in range(4):
        base = EGA_APERTURE + p * EGA_PLANE_STRIDE + CACHE_OFF
        planes.append(bytes(mem.data[base:base + CACHE_BYTES]))
    return planes


def main() -> int:
    playback = InputDemoPlayback.load(DEMO)
    meta = playback.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    fast_adlib = bool(meta.get("fast_adlib", False))
    rt = load_pre2_snapshot(
        ROOT / "assets" / "pre2.exe",
        playback.snapshot_path(),
        game_root=ROOT / "assets",
        fast_adlib=fast_adlib,
    )
    cpu = rt.cpu
    mem = cpu.mem
    cpu.trace_enabled = False

    hits = {a: 0 for a in (0x42F7, 0x4369, 0x436A, 0x43B2, 0x4213, 0x428F)}
    order = []
    cap = {}

    def probe(addr):
        def handler(c):
            hits[addr] += 1
            if len(order) < 40:
                order.append((addr, c.instruction_count))
            if addr == 0x42F7 and "local_in" not in cap:
                src, dd6, d86, mult = _src_seg_local(mem)
                cap["local_in"] = {
                    "src_seg": src, "dd6": dd6, "d86": d86, "mult": mult,
                    "shared_base": _rw(mem, DS, 0x2DD8),
                    "asset": bytes(mem.data[(src << 4):(src << 4) + 0x200 + 256 * 128]),
                    "index_table": bytes(mem.data[(src << 4):(src << 4) + 0x200]),
                }
            elif addr == 0x4369 and "local_in" in cap and "local_out" not in cap:
                cap["local_out"] = _cache_planes(mem)
                s = c.s
                cap["local_regs"] = {r: getattr(s, r) & 0xFFFF for r in
                                     ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")}
            elif addr == 0x436A and "shared_in" not in cap:
                base = _rw(mem, DS, 0x2DD8)
                cap["shared_in"] = {
                    "shared_base": base,
                    "index_copy": bytes(mem.data[(DS << 4) + 0x25CA:(DS << 4) + 0x25CA + 0x400]),
                    # generous: bank spans beyond 64K (max in-bank code ~0x315 => ~68K).
                    "asset": bytes(mem.data[(base << 4):(base << 4) + 0x18000]),
                }
            elif addr == 0x43B2 and "shared_in" in cap and "shared_out" not in cap:
                cap["shared_out"] = _cache_planes(mem)
                s = c.s
                cap["shared_regs"] = {r: getattr(s, r) & 0xFFFF for r in
                                      ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")}
            elif addr == 0x428F and "type_table" not in cap:
                cap["type_table"] = bytes(mem.data[(DS << 4) + 0x4DF4:(DS << 4) + 0x4DF4 + 256])
                cap["classify_cache"] = _cache_planes(mem)
            interpret_current_instruction_without_hook(c)
        return handler

    for addr in hits:
        cpu.replacement_hooks[(SEG, addr)] = probe(addr)
        cpu.hook_names[(SEG, addr)] = f"probe_{addr:04X}"

    # Keep running well past the last input event: the level load (and the sprite
    # decode) happens *after* the menu selection that the demo injects.
    frame = 0
    FRAME_CAP = 3000
    while frame < FRAME_CAP:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if "local_out" in cap and "type_table" in cap:
            if "done_frame" not in cap:
                cap["done_frame"] = frame
                print(f"captured decode+classify by frame {frame}")
            elif frame > cap["done_frame"] + 80:
                break
        frame += 1

    print("hit counts:", {f"{a:04X}": n for a, n in hits.items()})
    if "local_regs" in cap:
        print("42F7 exit regs:", {k: f"{v:04X}" for k, v in cap["local_regs"].items()})
    if "shared_regs" in cap:
        print("436A exit regs:", {k: f"{v:04X}" for k, v in cap["shared_regs"].items()})
    if "shared_in" in cap:
        print(f"shared_base={cap['shared_in']['shared_base']:04X} bank_bytes={len(cap['shared_in']['asset'])}")
    print("captured keys:", sorted(cap.keys()))
    if "local_in" in cap:
        ci = cap["local_in"]
        codes = [ci["index_table"][2 * i] | (ci["index_table"][2 * i + 1] << 8) for i in range(256)]
        nloc = sum(1 for c in codes if c < 0x100)
        nsh = sum(1 for c in codes if 0x100 <= c < 0x200)
        print(f"  local src_seg={ci['src_seg']:04X} dd6={ci['dd6']:04X} d86={ci['d86']:02X} mult={ci['mult']:02X}"
              f" shared_base={ci['shared_base']:04X}")
        print(f"  index codes: local<0x100={nloc} shared0x100..0x1FF={nsh} first16={[hex(c) for c in codes[:16]]}")

    if "local_in" in cap and "local_out" in cap:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "local_asset.bin").write_bytes(cap["local_in"]["asset"])
        for p in range(4):
            (OUT / f"local_cache_plane{p}.bin").write_bytes(cap["local_out"][p])
        if "type_table" in cap:
            (OUT / "type_table.bin").write_bytes(cap["type_table"])
            for p in range(4):
                (OUT / f"classify_cache_plane{p}.bin").write_bytes(cap["classify_cache"][p])
        if "shared_in" in cap:
            (OUT / "shared_asset.bin").write_bytes(cap["shared_in"]["asset"])
            (OUT / "shared_index_copy.bin").write_bytes(cap["shared_in"]["index_copy"])
            if "shared_out" in cap:
                for p in range(4):
                    (OUT / f"shared_cache_plane{p}.bin").write_bytes(cap["shared_out"][p])
        meta_out = {k: v for k, v in cap["local_in"].items() if isinstance(v, int)}
        (OUT / "meta.json").write_text(json.dumps(meta_out, indent=2))
        print(f"wrote witness to {OUT}")
        return 0
    print("FAILED to capture decode (did the demo reach level load?)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
