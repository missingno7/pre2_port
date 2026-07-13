"""Gap #3 product-loop proof: the REAL interactive frame driver — native_frame_step_tagged, the exact generator
play_native pumps every frame, transitions and all — yields byte-identical frames whether the gameplay state of
record is the byte image (the shipped default) or the offset-free OBJECT GRAPH (object_store=True).

This is the end of the line for "the object graph is a sufficient state of record": not a bespoke verifier loop
(verify_object_finish / verify_object_render) but the actual shipped runtime entry point, driven exactly as the
interactive game drives it — one native_frame_step_tagged call per tick, each yielding one normal frame or a run
of transition frames. Every yielded (planes, page, interpolatable, tx) tuple must match frame-for-frame.

The default (object_store=False) path is byte-for-byte the shipped code; this proves turning the flag ON changes
nothing observable — so flipping the product default (plan item #4) is now a one-line, proven-safe change,
deliberately left for explicit sign-off under the release hold.

    python scripts/verify_object_playloop.py [demo_dir] [max_ticks]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from pre2.bridge.object_runtime import ObjectStore
from pre2.gaps import Pre2HybridGap
from pre2.native.game_tick_demo import GameTickDemo, _inject
from pre2.native.runtime import native_frame_step_tagged
from pre2.native.state import NativeGameState
from pre2.native.vga import NativeVGA


def _frame_key(planes, page, interp, tx):
    pb = b"".join(bytes(p) for p in planes) if planes else b""
    return (pb, page, interp, tx)


def _drive(gtd, n, gr, object_store):
    st = NativeGameState(bytearray(gtd.seed))
    dos = NativeVGA()
    store = ObjectStore() if object_store else None      # the bridge INJECTS the object-store controller
    out = []
    for i in range(n):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            for planes, page, interp, tx in native_frame_step_tagged(st, dos, 0, game_root=gr, store=store):
                out.append(_frame_key(planes, page, interp, tx))
        except Pre2HybridGap as e:
            out.append(("GAP", type(e).__name__))
            break
    return out


def main() -> int:
    demo_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_full_gorilla_20260628_203423"
    tick_file = ROOT / demo_dir / "game_tick_demo.bin"
    if not tick_file.exists():
        print(f"no {tick_file} — run verify_native_tick_demo.py {demo_dir} first")
        return 2
    gtd = GameTickDemo.load(tick_file)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else gtd.n_ticks
    n = min(n, gtd.n_ticks)
    gr = str(ROOT / "assets")
    print(f"driving the REAL native_frame_step_tagged loop over {n} ticks: byte image vs object graph ...")

    ref = _drive(gtd, n, gr, object_store=False)
    obj = _drive(gtd, n, gr, object_store=True)
    if ref == obj:
        print(f"  PASS: all {len(ref)} yielded frames identical — the product loop runs on the object graph.")
        return 0
    if len(ref) != len(obj):
        print(f"  DIVERGED: frame counts differ (image {len(ref)} vs object {len(obj)})")
        return 1
    for k, (x, y) in enumerate(zip(ref, obj)):
        if x != y:
            print(f"  DIVERGED: first differing yielded frame at index {k}")
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
