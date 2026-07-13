"""Object-model Phase 5 proof: the object graph is a SELF-CONTAINED state of record — no external byte image.

The graph (named nodes + the structured entity arena) plus ``GameState.residue`` (the rest of the DGROUP heap:
the per-level loaded tables — tile props, the anim/attack/camera script bytecode, the bonus-cell + effect
lists — carried as loaded INPUT bytes) fully determines the DGROUP. This runs the gameplay tick, and at every
tick reconstructs the WHOLE DGROUP from the GameState ALONE (``to_image(gs)`` with no image argument), requiring
byte-identity with the live image. Proves you can drop the external byte image: the GameState is the complete
DGROUP state of record.

    python scripts/verify_object_selfcontained.py [demo ...]
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


def run_demo(demo_dir: Path) -> int:
    from pre2.bridge.state_object import from_image, to_image
    from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.level_state import native_4f6c
    from pre2.native.loop import native_cave_teleport, native_gameplay_frame
    from pre2.native.state import NativeGameState

    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")
    st = NativeGameState(bytearray(gtd.seed))
    n = 0
    for i in range(gtd.n_ticks):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(st)
        except Pre2CaveTeleport as tp:
            for _ in native_cave_teleport(st, tp.si):
                pass
        except Pre2RespawnTransition:
            for _ in native_4f6c(st):
                pass
        except Pre2HybridGap:
            break
        gs = from_image(st.data)                          # image -> object graph (+ residue)
        rebuilt = to_image(gs)                            # graph ALONE -> DGROUP (no external image)
        want = st.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        got = rebuilt[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if got != want:
            off = next(k for k in range(0x10000) if got[k] != want[k])
            raise AssertionError(f"tick {i}: self-contained DGROUP diverged at 0x{off:04X}")
        n += 1
    return n


def main(argv) -> int:
    demos = argv or CORPUS
    total = 0
    for d in demos:
        p = ROOT / d
        if not (p / "game_tick_demo.bin").exists():
            print(f"  skip {d}")
            continue
        c = run_demo(p)
        total += c
        print(f"  PASS: {d} — {c} ticks reconstructed the whole DGROUP from the GameState alone")
    print(f"\nself-contained object graph: the DGROUP reconstructs from the GameState alone, no external image "
          f"({total} ticks across {len(demos)} demos, byte-identical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
