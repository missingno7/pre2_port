"""Gate: the game state is a lossless object graph (object-model Phase 1).

Runs the object-graph round-trip (image -> GameState -> image, byte-identical on the named region) on one
demo in the standard test run; scripts/verify_object_roundtrip.py is the full-corpus proof.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DGROUP_BASE = 0x1A0F << 4


def test_object_graph_round_trips_the_named_region():
    from pre2.bridge.state_fields import named_bytes
    from pre2.bridge.state_object import from_image, to_image
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")

    named = named_bytes()
    gtd = GameTickDemo.load(demo)
    state = NativeGameState(bytearray(gtd.seed))

    def check():
        gs = from_image(state.data)
        scratch = bytearray(len(state.data))
        to_image(gs, scratch)
        for o in named:
            assert state.data[DGROUP_BASE + o] == scratch[DGROUP_BASE + o], f"round-trip mismatch at 0x{o:04X}"

    check()
    for i in range(min(gtd.n_ticks, 15)):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(state)
        check()
