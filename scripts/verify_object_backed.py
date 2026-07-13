"""Object-model Phase 3 proof: the gameplay tick RUNS on the object graph as its live store.

Runs each corpus demo TWICE in lockstep — once on the normal ByteBackend, once on the
``ObjectGraphBackend`` (named state in per-node buckets + the entity-arena bucket, residue in an image) — and
requires the whole DGROUP image to match every tick. Proves the structured object graph is a sufficient LIVE
state of record for the gameplay tick, not merely a lossless snapshot: the game runs on it with nothing else
changed, byte-identical.

    python scripts/verify_object_backed.py [demo ...]     (default: the standing corpus)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "dos_re"))

DGROUP_BASE = 0x1A0F << 4

CORPUS = ["artifacts/demo_pre2_full_gorilla_20260628_203423", "artifacts/demo_pre2_20260706_020106",
          "artifacts/demo_pre2_20260712_121135", "artifacts/demo_pre2_finish_game_norepl_20260703_165400",
          "artifacts/demo_cold_20260712_172030"]


def _step(state, keys, idle, i):
    from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition
    from pre2.native.game_tick_demo import _inject
    from pre2.native.level_state import native_4f6c
    from pre2.native.loop import native_cave_teleport, native_gameplay_frame
    _inject(state, keys[i], idle[i] if i < len(idle) else None)
    try:
        native_gameplay_frame(state)
    except Pre2CaveTeleport as tp:
        for _ in native_cave_teleport(state, tp.si):
            pass
    except Pre2RespawnTransition:
        for _ in native_4f6c(state):
            pass
    except Pre2HybridGap:
        return False
    return True


def run_demo(demo_dir: Path) -> int:
    from pre2.bridge.state_object import ObjectGraphBackend
    from pre2.native.game_tick_demo import GameTickDemo
    from pre2.native.state import NativeGameState

    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")

    ref = NativeGameState(bytearray(gtd.seed))                 # the normal byte-image run
    obj = NativeGameState(bytearray(gtd.seed))                 # the object-graph-backed run
    obj.backend = ObjectGraphBackend(obj)                      # named state -> per-node buckets

    ticks = 0
    for i in range(gtd.n_ticks):
        ok_ref = _step(ref, gtd.keys, gtd.idle, i)
        ok_obj = _step(obj, gtd.keys, gtd.idle, i)
        if ok_ref != ok_obj:
            raise AssertionError(f"tick {i}: gap divergence (ref={ok_ref} obj={ok_obj})")
        if not ok_ref:
            break
        obj.backend.materialize()                              # fold the buckets back for the compare
        a = ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        b = obj.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if a != b:
            off = next(k for k in range(0x10000) if a[k] != b[k])
            raise AssertionError(f"tick {i}: object-backed run diverged at 0x{off:04X} "
                                 f"(byte {a[off]:#04x} vs {b[off]:#04x})")
        ticks += 1
    return ticks


def main(argv) -> int:
    demos = argv or CORPUS
    total = 0
    for d in demos:
        p = ROOT / d
        if not (p / "game_tick_demo.bin").exists():
            print(f"  skip {d}")
            continue
        n = run_demo(p)
        total += n
        print(f"  PASS: {d} — {n} ticks ran on the object graph, byte-identical to the image run")
    print(f"\nobject-backed tick: the gameplay loop runs on the structured object graph "
          f"({total} ticks across {len(demos)} demos, byte-identical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
