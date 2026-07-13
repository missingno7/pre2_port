"""Gate: the gameplay tick runs on the object-graph store byte-identically (object-model Phase 3).

Runs one demo on the ObjectGraphBackend vs the normal ByteBackend in lockstep; scripts/verify_object_backed.py
is the full-corpus proof.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DGROUP_BASE = 0x1A0F << 4


def test_gameplay_tick_runs_on_the_object_graph():
    from pre2.bridge.state_object import ObjectGraphBackend
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")

    gtd = GameTickDemo.load(demo)
    ref = NativeGameState(bytearray(gtd.seed))
    obj = NativeGameState(bytearray(gtd.seed))
    obj.backend = ObjectGraphBackend(obj)

    for i in range(min(gtd.n_ticks, 15)):
        idle = gtd.idle[i] if i < len(gtd.idle) else None
        _inject(ref, gtd.keys[i], idle); native_gameplay_frame(ref)
        _inject(obj, gtd.keys[i], idle); native_gameplay_frame(obj)
        obj.backend.materialize()
        assert ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000] == obj.data[DGROUP_BASE:DGROUP_BASE + 0x10000], \
            f"object-backed run diverged at tick {i}"


def test_object_graph_is_self_contained():
    """The GameState (nodes + arena + residue) reconstructs the whole DGROUP with no external image."""
    from pre2.bridge.state_object import from_image, to_image
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")

    gtd = GameTickDemo.load(demo)
    st = NativeGameState(bytearray(gtd.seed))
    for i in range(min(gtd.n_ticks, 15)):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(st)
        rebuilt = to_image(from_image(st.data))          # graph ALONE, no external image
        assert rebuilt[DGROUP_BASE:DGROUP_BASE + 0x10000] == st.data[DGROUP_BASE:DGROUP_BASE + 0x10000], \
            f"self-contained DGROUP diverged at tick {i}"
