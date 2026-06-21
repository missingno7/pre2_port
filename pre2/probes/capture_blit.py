"""TEMPORARY probe — capture a faithful per-blit witness.

Replays into gameplay and, for the first blit call of each type (0 plain / 1 empty
/ >=2 masked), transparently runs the original blit at 1030:3B88 to completion and
records the 4 EGA planes before+after plus all inputs (idx, di, es, type, the
[0x2DF8] mask, the [0x2DF6]/[0x6BC4] bg-restore state). This is the verification
target for the recovered renderer (pre2/recovered/renderer.py).

Run:  python -m pre2.probes.capture_blit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
OUT = ROOT / "artifacts" / "blit_witness"
SEG = 0x1030
DS = 0x1A0F
BLIT = (SEG, 0x3B88)
WIN = 0x10000  # full EGA plane (covers visible+offscreen+cache+bg source)


def _planes(mem) -> list[bytes]:
    return [bytes(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + WIN])
            for p in range(4)]


def _run_to_return(cpu) -> None:
    """Transparently execute the blit routine to its near-ret and stop after it."""
    entry_sp = cpu.s.sp & 0xFFFF
    fn = cpu.replacement_hooks.pop(BLIT, None)
    name = cpu.hook_names.pop(BLIT, None)
    try:
        # step until the routine's own ret pops the return address (sp rises above entry).
        for _ in range(200000):
            cpu.step()
            if (cpu.s.sp & 0xFFFF) > entry_sp:
                break
    finally:
        if fn is not None:
            cpu.replacement_hooks[BLIT] = fn
        if name is not None:
            cpu.hook_names[BLIT] = name


def main() -> int:
    playback = InputDemoPlayback.load(DEMO)
    meta = playback.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(
        ROOT / "assets" / "pre2.exe", playback.snapshot_path(),
        game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)),
    )
    cpu = rt.cpu
    mem = cpu.mem
    cpu.trace_enabled = False

    captured: dict[int, dict] = {}   # type-bucket -> witness
    counts = {0: 0, 1: 0, 2: 0}

    def handler(c):
        idx = c.s.ax & 0xFF
        typ = mem.data[(DS << 4) + 0x4DF8 + idx]
        bucket = 0 if typ == 0 else (1 if typ == 1 else 2)
        counts[bucket] += 1
        # skip degenerate early calls before the scroll background is set up.
        grab = bucket not in captured and mem.rw(DS, 0x2DF6) != 0
        info = None
        if grab:
            di = c.s.di & 0xFFFF
            info = {
                "type": typ, "idx": idx, "di": di, "es": c.s.es & 0xFFFF,
                "df2": mem.rw(DS, 0x2DF6), "bc0": mem.data[(DS << 4) + 0x6BC4],
                "mask": bytes(mem.data[(DS << 4) + 0x2DF8 + (typ - 2) * 0x20:
                                       (DS << 4) + 0x2DF8 + (typ - 2) * 0x20 + 0x20]) if typ >= 2 else b"",
                "before": _planes(mem),
            }
        _run_to_return(c)
        if grab:
            info["after"] = _planes(mem)
            captured[bucket] = info
        return

    cpu.replacement_hooks[BLIT] = handler
    cpu.hook_names[BLIT] = "blit_capture"

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if len(captured) == 3:
            print(f"captured all 3 blit types by frame {frame}")
            break
        frame += 1

    print("blit call counts by bucket:", counts)
    print("captured types:", sorted(captured))
    if captured:
        OUT.mkdir(parents=True, exist_ok=True)
        index = {}
        for bucket, info in captured.items():
            for p in range(4):
                (OUT / f"t{bucket}_before_p{p}.bin").write_bytes(info["before"][p])
                (OUT / f"t{bucket}_after_p{p}.bin").write_bytes(info["after"][p])
            if info["mask"]:
                (OUT / f"t{bucket}_mask.bin").write_bytes(info["mask"])
            index[bucket] = {k: (v if isinstance(v, int) else None)
                             for k, v in info.items() if k in ("type", "idx", "di", "es", "df2", "bc0")}
            # diff summary
            changed = sum(1 for p in range(4) for k in range(WIN)
                          if info["before"][p][k] != info["after"][p][k])
            index[bucket]["changed_bytes"] = changed
            print(f"  type {info['type']} idx={info['idx']} di={info['di']:04X} es={info['es']:04X} "
                  f"df2={info['df2']:04X} bc0={info['bc0']:02X} changed_bytes={changed}")
        (OUT / "index.json").write_text(json.dumps(index, indent=2))
        print(f"wrote witness to {OUT}")
    return 0 if len(captured) == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
