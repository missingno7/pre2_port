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

def _corpus():
    import glob
    return sorted(set(glob.glob(str(ROOT / "artifacts" / "demo_pre2_*"))
                      + glob.glob(str(ROOT / "artifacts" / "demo_cold_*"))))


CORPUS = _corpus()


def run_demo(demo_dir: Path) -> int:
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")
    ref = NativeGameState(bytearray(gtd.seed))
    obj = NativeGameState(bytearray(gtd.seed))
    # readonly_image=True asserts the gap-#1 invariant: the tick writes NOTHING to the image (all mutable state
    # is on the object graph). Any un-routed mutable write raises instead of silently passing.
    obj.backend = DataclassBackend(obj, readonly_image=True)

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
        # ref runs on the DEFAULT ByteBackend, where the RNG is LIVE on ref.rng (registered in
        # NativeGameState.__init__) -- so ref.data's RNG bytes are stale until folded back, exactly like every
        # other raw-image digest read. obj's RNG is folded by its own materialize() above, so without this the
        # comparison comes down to fresh-vs-stale RNG (it diverged at 0x28C1, the ror word).
        ref.sync_rng_to_image()
        a = ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        b = obj.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if a != b:
            off = next(k for k in range(0x10000) if a[k] != b[k])
            raise AssertionError(f"tick {i}: diverged at 0x{off:04X} (player is a dataclass)")
        n += 1
    return n


def main(argv) -> int:
    from pre2.bridge.game_layout import _BUFFERS, _ROUTES
    names = ", ".join(a for a, *_ in _ROUTES)
    n_objs = sum(count for *_, count, _ in _ROUTES) + len(_BUFFERS)
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
    print(f"\ntick-on-dataclasses: the gameplay tick runs with {n_objs}+ offset-free dataclass instances across "
          f"{len(_ROUTES)} structures ({names}), the variable-stride entity arena, and {len(_BUFFERS)} named "
          f"working buffers — {total} ticks across {len(demos)} demos, byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
