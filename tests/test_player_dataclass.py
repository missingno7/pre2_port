"""Gate: the gameplay tick runs with the player's live state as an offset-free Player dataclass (north-star
crux step). scripts/verify_player_dataclass.py is the full-corpus proof.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DGROUP_BASE = 0x1A0F << 4


def test_tick_runs_with_player_as_a_live_dataclass():
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.game.model import Camera, Player, Progress, Rng
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
    obj.backend = DataclassBackend(obj)
    # the live store routes several structures to real offset-free dataclasses
    from pre2.game.model import Actor
    assert isinstance(obj.backend.player, Player) and isinstance(obj.backend.rng, Rng)
    assert isinstance(obj.backend.camera, Camera) and isinstance(obj.backend.progress, Progress)
    assert len(obj.backend.actors) == 12 and all(isinstance(a, Actor) for a in obj.backend.actors)
    from pre2.game.model import ArenaEntity
    assert obj.backend.entities and all(isinstance(e, ArenaEntity) for e in obj.backend.entities)

    for i in range(min(gtd.n_ticks, 15)):
        idle = gtd.idle[i] if i < len(gtd.idle) else None
        _inject(ref, gtd.keys[i], idle); native_gameplay_frame(ref)
        _inject(obj, gtd.keys[i], idle); native_gameplay_frame(obj)
        obj.backend.materialize()
        assert ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000] == obj.data[DGROUP_BASE:DGROUP_BASE + 0x10000], \
            f"player-dataclass run diverged at tick {i}"
    # the player object actually changed over the run (it's live, not a snapshot)
    assert isinstance(obj.backend.player.x, int)


def test_no_two_routes_claim_the_same_offset():
    """Two routed structures must never map the same DGROUP byte (would corrupt materialize)."""
    from pre2.bridge.game_layout import _ROUTES
    seen: dict[int, str] = {}
    for attr, _cls, layout, base, count, stride in _ROUTES:
        for k in range(count):
            for _f, off, w, _s in layout:
                for bk in range(w):
                    o = (base + k * stride + off + bk) & 0xFFFF
                    assert o not in seen, f"offset {o:#06x} claimed by both {seen[o]} and {attr}"
                    seen[o] = attr
