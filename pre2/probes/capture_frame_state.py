"""TEMPORARY probe — witness the frame-renderer camera/scroll state.

Records the scroll-engine state block (ds=1A13) every frame of a demo replay and
summarises how each field moves: the range it spans, whether it wraps, and at what
modulus. This validates the field semantics inferred from disassembly (see the
"frame renderer / scroll engine" section of docs/pre2/symbol_ledger.md) BEFORE we
commit a Camera/ScrollState/TileMap dataclass to pre2/bridge/.

It does NOT replace any routine — it just observes.
Retire when: the Camera/TileMap bridge fields are stable (kept meanwhile as the
witness-regeneration tool behind tests/test_frame_bridge.py). Run:
    python -m pre2.probes.capture_frame_state
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
DS = 0x1A13

# (name, offset, width)  — the inferred Camera/ScrollState/TileMap fields.
FIELDS = [
    ("camera_x",      0x2DE0, 2),   # camera column (tiles)
    ("camera_y",      0x2DE2, 2),   # camera row (tiles)
    ("prev_camera_x", 0x2DDC, 2),   # dirty-compare previous camera
    ("prev_camera_y", 0x2DDE, 2),
    ("col_ring_idx",  0x2DE4, 1),   # column ring index (0..0x13)
    ("row_ring_idx",  0x2DE6, 1),   # row ring index (0..0xB / 0xC)
    ("fine_scroll",   0x6BC0, 1),   # sub-tile pixel scroll (0..0x10)
    ("row_factor",    0x6BF4, 1),   # row-stride factor used by 3A08/3582
    ("scroll_src",    0x2DB6, 2),   # scroll source offset (computed by 3569)
    ("dest_a",        0x2DD2, 2),
    ("dest_b",        0x2DD4, 2),
    ("sheet_seg",     0x2DD6, 2),
    ("level_height",  0x2CF1, 1),   # level height in rows
    ("dirty0",        0x2DF0, 1),
    ("dirty1",        0x2DF1, 1),
]


def _read(mem, off, width):
    base = ((DS << 4) + off) & 0xFFFFF
    return mem.data[base] if width == 1 else mem.data[base] | (mem.data[base + 1] << 8)


def main() -> int:
    playback = InputDemoPlayback.load(DEMO)
    meta = playback.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(
        ROOT / "assets" / "pre2.exe", playback.snapshot_path(),
        game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)),
    )
    cpu = rt.cpu
    cpu.trace_enabled = False

    # Optional: hold a movement key every frame to force the camera to scroll
    # (e.g. `... capture_frame_state 4D` holds Right). Without it the demo's own
    # input drives the run.
    hold = int(sys.argv[1], 16) if len(sys.argv) > 1 else None
    nframes = int(sys.argv[2]) if len(sys.argv) > 2 else 1500

    rows: list[dict[str, int]] = []
    frame = 0
    while frame < nframes:
        if hold is None:
            playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        else:
            deliver_scancode(rt, hold, max_steps=2000)  # make code = key held this frame
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        rows.append({name: _read(cpu.mem, off, w) for name, off, w in FIELDS})
        frame += 1

    if not rows:
        print("no frames captured")
        return 1

    print(f"captured {len(rows)} frames from {DEMO.name}\n")
    print(f"{'field':<14} {'min':>6} {'max':>6} {'changes':>8}  sample")
    for name, _off, _w in FIELDS:
        vals = [r[name] for r in rows]
        changes = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        # first few distinct transitions as a movement fingerprint
        seq, last = [], None
        for v in vals:
            if v != last:
                seq.append(v)
                last = v
            if len(seq) >= 12:
                break
        print(f"{name:<14} {min(vals):>6} {max(vals):>6} {changes:>8}  "
              + " ".join(f"{v:X}" for v in seq))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
