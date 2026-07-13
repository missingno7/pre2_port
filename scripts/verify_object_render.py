"""Gap #3 render proof: the REAL product render loop — a gameplay tick FOLLOWED BY the full native_render
EVERY frame — produces byte-identical VGA planes whether the state of record is the byte image or the
offset-free OBJECT GRAPH.

verify_object_full.py renders ONCE after N ticks; the shipped product (native_frame_step_tagged) renders every
frame, so render state that persists between frames matters. This drives the exact functions the product loop
calls — native_gameplay_frame (the tick) then native_sync_render_state + native_render (the frame) — with the
state on the object store, and compares the emitted planes frame-for-frame against the byte-image baseline.

The deployment model it proves:
  - the gameplay tick runs on the object graph (the state of record);
  - object_runtime.materialize folds gameplay state into the image, PRESERVING the render-owned counters
    (RENDER_COUNTERS) that native_render steps and that persist across frames;
  - the render then runs over that image (its read-modify-restore scratch — flash bits, camera mirrors — must
    not touch the object graph, so render uses a plain ByteBackend view of the materialised image).

If the planes match every frame across a whole demo, the object graph is a sufficient gameplay state of record
for the real render loop — the image is only ever a materialised render buffer.

    python scripts/verify_object_render.py [demo_dir] [max_ticks]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from pre2.bridge.game_layout import DataclassBackend
from pre2.bridge.object_runtime import materialize
from pre2.gaps import Pre2HybridGap
from pre2.native.game_tick_demo import GameTickDemo, _inject
from pre2.native.loop import native_gameplay_frame
from pre2.native.render import native_render, native_sync_render_state
from pre2.native.state import NativeGameState
from pre2.native.vga import NativeVGA
from pre2.views.dgroup_view import ByteBackend


def _planes(planes) -> bytes:
    return b"".join(bytes(p) for p in planes)


def _ref_frames(gtd, n, gr):
    st = NativeGameState(bytearray(gtd.seed))
    dos = NativeVGA()
    out = []
    for i in range(n):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(st)                            # image-backed tick
        native_sync_render_state(st)
        p, _ = native_render(st, dos, 0, game_root=gr, force_gameplay=True)
        out.append(_planes(p))
    return out


def _obj_frames(gtd, n, gr):
    st = NativeGameState(bytearray(gtd.seed))
    dos = NativeVGA()
    obj = DataclassBackend(st, readonly_image=True)
    out = []
    for i in range(n):
        st.backend = obj                                     # tick on the object graph (state of record)
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(st)
        materialize(st)                                      # fold gameplay -> image (render counters preserved)
        st.backend = ByteBackend(st)                         # render over the image; scratch writes stay off the graph
        native_sync_render_state(st)
        p, _ = native_render(st, dos, 0, game_root=gr, force_gameplay=True)
        out.append(_planes(p))
    return out


def main() -> int:
    demo_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_full_gorilla_20260628_203423"
    tick_file = ROOT / demo_dir / "game_tick_demo.bin"
    if not tick_file.exists():
        print(f"no {tick_file} — run verify_native_tick_demo.py {demo_dir} first")
        return 2
    gtd = GameTickDemo.load(tick_file)
    # stop before the first transition (this proof is the per-frame render path; transitions are proven by
    # verify_object_finish). Probe how far the pure-gameplay run goes.
    probe = NativeGameState(bytearray(gtd.seed))
    probe.backend = DataclassBackend(probe, readonly_image=True)
    n = 0
    for i in range(gtd.n_ticks):
        _inject(probe, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(probe)
        except Pre2HybridGap:
            break
        n += 1
    if len(sys.argv) > 2:
        n = min(n, int(sys.argv[2]))
    print(f"verifying the every-frame RENDER loop on the object graph over {n} gameplay frames ...")

    gr = str(ROOT / "assets")
    ref = _ref_frames(gtd, n, gr)
    obj = _obj_frames(gtd, n, gr)
    if ref == obj:
        print(f"  PASS: all {n} rendered frames are byte-identical on the object graph (image = render buffer).")
        return 0
    for k, (x, y) in enumerate(zip(ref, obj)):
        if x != y:
            print(f"  DIVERGED: first differing rendered frame at tick {k}")
            return 1
    print("  DIVERGED: frame counts differ")
    return 1


if __name__ == "__main__":
    sys.exit(main())
