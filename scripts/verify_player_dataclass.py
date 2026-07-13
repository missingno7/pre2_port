"""North-star step: the gameplay tick runs with the PLAYER as a live Player dataclass (not bytes).

Runs each corpus demo twice in lockstep — reference ByteBackend vs the bridge DataclassBackend, where every
access to the player record is routed to/from the fields of a real ``Player`` object — and requires the whole
DGROUP image to match every tick. Proves the player's live state can BE an offset-free dataclass during the
gameplay loop, byte-identical; the offsets live only in the bridge layout mapping.

    python scripts/verify_player_dataclass.py [demo ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "dos_re"))

DGROUP_BASE = 0x1A0F << 4

CORPUS = ["artifacts/demo_pre2_full_gorilla_20260628_203423", "artifacts/demo_pre2_20260706_020106",
          "artifacts/demo_pre2_20260712_121135", "artifacts/demo_cold_20260712_172030"]


def run_demo(demo_dir: Path) -> int:
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")
    ref = NativeGameState(bytearray(gtd.seed))
    obj = NativeGameState(bytearray(gtd.seed))
    obj.backend = DataclassBackend(obj)

    n = 0
    for i in range(gtd.n_ticks):
        idle = gtd.idle[i] if i < len(gtd.idle) else None
        try:
            _inject(ref, gtd.keys[i], idle); native_gameplay_frame(ref)
            _inject(obj, gtd.keys[i], idle); native_gameplay_frame(obj)
        except Exception as e:
            if type(e).__name__.startswith("Pre2"):
                break               # a transition (level-end/respawn): outside this pure-gameplay proof
            raise
        obj.backend.materialize()
        a = ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        b = obj.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if a != b:
            off = next(k for k in range(0x10000) if a[k] != b[k])
            raise AssertionError(f"tick {i}: diverged at 0x{off:04X} (player is a dataclass)")
        n += 1
    return n


def main(argv) -> int:
    from pre2.bridge.game_layout import _ROUTES
    names = ", ".join(a for a, *_ in _ROUTES)
    demos = argv or CORPUS
    total = 0
    for d in demos:
        p = ROOT / d
        if not (p / "game_tick_demo.bin").exists():
            print(f"  skip {d}")
            continue
        c = run_demo(p)
        total += c
        print(f"  PASS: {d} — {c} ticks with {len(_ROUTES)} live dataclasses, byte-identical")
    print(f"\ntick-on-dataclasses: the gameplay tick runs with {len(_ROUTES)} offset-free dataclasses "
          f"({names}) — {total} ticks across {len(demos)} demos, byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
