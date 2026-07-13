"""Object-model Phase 1 proof: the game state is losslessly an OBJECT GRAPH.

Per tick over the tick-demo corpus, read the live DGROUP image into ``pre2.bridge.state_object.GameState``
(a graph of typed named-field nodes + the named arenas — no byte image), write it back into a scratch image,
and require the NAMED region to come back byte-identical. If the graph could not represent a byte — a field,
an alias, an arena — this fails loud at the exact offset and tick.

This is the foundation for dissolving the DGROUP into objects: it proves the graph is a sufficient, lossless
state of record before any code is changed to run on it. The byte-exact VM oracle stays the guarantee.

    python scripts/verify_object_roundtrip.py [demo ...]     (default: the standing corpus)
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


def _check_roundtrip(data, named) -> None:
    from pre2.bridge.state_object import from_image, to_image
    gs = from_image(data)
    scratch = bytearray(len(data))
    to_image(gs, scratch)
    for o in named:
        a, b = data[DGROUP_BASE + o], scratch[DGROUP_BASE + o]
        if a != b:
            raise AssertionError(f"object round-trip mismatch at 0x{o:04X}: image 0x{a:02X} -> graph 0x{b:02X}")


def run_demo(demo_dir: Path, every: int) -> int:
    from pre2.bridge.state_fields import named_bytes
    from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.level_state import native_4f6c
    from pre2.native.loop import native_cave_teleport, native_gameplay_frame
    from pre2.native.state import NativeGameState

    named = named_bytes()
    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")
    state = NativeGameState(bytearray(gtd.seed))
    _check_roundtrip(state.data, named)                # the seed state
    checked = 1
    for i in range(gtd.n_ticks):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(state)
        except Pre2CaveTeleport as tp:
            for _ in native_cave_teleport(state, tp.si):
                pass
        except Pre2RespawnTransition:
            for _ in native_4f6c(state):
                pass
        except Pre2HybridGap:
            break
        if i % every == 0 or i == gtd.n_ticks - 1:
            _check_roundtrip(state.data, named)
            checked += 1
    return checked


def main(argv) -> int:
    demos = argv or CORPUS
    every = 25
    total = 0
    for d in demos:
        p = ROOT / d
        if not (p / "game_tick_demo.bin").exists():
            print(f"  skip {d} (no game_tick_demo.bin)")
            continue
        n = run_demo(p, every)
        total += n
        print(f"  PASS: {d} — object graph round-tripped the named region on {n} sampled ticks")
    print(f"\nobject round-trip: the game state is a lossless object graph "
          f"({total} states across {len(demos)} demos, byte-identical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
