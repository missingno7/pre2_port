"""Phase 4 progress diagnostic: how far the gameplay tick runs with named state held in a FieldBackend
(OFF the byte image).

Two NativeGameStates from one demo seed, run through the identical tick sequence:
  * REFERENCE — the normal ByteBackend (state IS the image; already byte-exact vs the VM oracle).
  * HYBRID    — state.backend swapped to a HybridBackend: named DGROUP bytes live in a FieldBackend, only
                the residue stays in the image. After each tick the hybrid is MATERIALISED back over its
                image and the DGROUP window is compared to the reference.

When every tick matches, the FieldBackend faithfully held all named mutable state AND the tick never read a
named offset from the (now stale) image — the named store IS the product's state of record. Until then, the
FIRST diverging offset names the next native module still doing raw ``.data`` DGROUP access (the seam does
not yet cover it) — the flip's remaining work list. The infrastructure (the swappable backend seam,
HybridBackend, backend-aware readers/coerce, the walker + render-record routing) is proven byte-exact by the
normal corpus; this tool measures how much of the native layer already follows the seam.

    python scripts/verify_hybrid_tick.py [demo ...]      (default: the standing corpus's fast ones)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "dos_re"))

DGROUP_BASE = 0x1A0F << 4

CORPUS = ["artifacts/demo_pre2_20260712_121135", "artifacts/demo_pre2_20260706_020106",
          "artifacts/demo_pre2_full_gorilla_20260628_203423", "artifacts/demo_cold_20260712_172030"]


def _run_one_tick(state, gtd, i):
    from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition
    from pre2.native.game_tick_demo import _inject
    from pre2.native.level_state import native_4f6c
    from pre2.native.loop import native_cave_teleport, native_gameplay_frame

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
        return False
    return True


def run_demo(demo_dir: Path) -> int:
    from pre2.native.game_tick_demo import GameTickDemo
    from pre2.native.state import NativeGameState
    from pre2.views.dgroup_view import FieldBackend, HybridBackend

    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")
    ref = NativeGameState(bytearray(gtd.seed))
    hyb = NativeGameState(bytearray(gtd.seed))
    hb = HybridBackend(FieldBackend(hyb), hyb.data)   # named state -> the FieldBackend; residue -> hyb.data
    hyb.backend = hb

    n = 0
    for i in range(gtd.n_ticks):
        ok_ref = _run_one_tick(ref, gtd, i)
        ok_hyb = _run_one_tick(hyb, gtd, i)
        if ok_ref != ok_hyb:
            raise AssertionError(f"tick {i}: gap divergence (ref {ok_ref} vs hybrid {ok_hyb})")
        if not ok_ref:
            break
        hb.materialize(hyb.data)                       # fold the FieldBackend's named bytes back into the image
        a = ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        b = hyb.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if a != b:
            off = next(k for k in range(0x10000) if a[k] != b[k])
            from pre2.views.dgroup_view import _named_map
            owner = _named_map().get(off, ("residue (unnamed)",))[0]
            return i, (off, a[off], b[off], owner)
        n = i + 1
    return n, None


def main() -> int:
    demos = sys.argv[1:] or CORPUS
    print("Phase 4 diagnostic: how far the tick runs with named state in a FieldBackend (off the image) ...")
    frontier = False
    for d in demos:
        p = ROOT / d
        if not (p / "game_tick_demo.bin").exists():
            print(f"  (skipping {d}: no tick bin)")
            continue
        n, diverge = run_demo(p)
        if diverge is None:
            print(f"  FULL {p.name}: {n} ticks -- hybrid == reference every tick (named state lived in the FieldBackend)")
        else:
            frontier = True
            off, va, vb, owner = diverge
            print(f"  FRONTIER {p.name}: {n} ticks clean, then 0x{off:04X} ({owner}) diverged "
                  f"(ref 0x{va:02X} != hybrid 0x{vb:02X}) -- a native module still accesses it via raw .data")
    print("\nThe seam infrastructure is proven byte-exact by the normal corpus; the frontier above is the next"
          if frontier else "PROVEN: the named field store is a sufficient state of record for the gameplay tick.")
    if frontier:
        print("native module to route through state.backend (the flip's remaining work).")
    return 1 if frontier else 0


if __name__ == "__main__":
    raise SystemExit(main())
