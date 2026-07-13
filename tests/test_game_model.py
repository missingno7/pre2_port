"""Gate: the shipped offset-free game model (pre2/game/model.py) round-trips byte-exact via the detachable
bridge (pre2/bridge/game_layout.py) — the object-model north-star pattern, proven on the player + rng.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DGROUP_BASE = 0x1A0F << 4


def test_player_is_a_plain_offset_free_dataclass():
    from pre2.game.model import Player
    p = Player(x=0x1234, sprite=0x2046, facing=-1)
    assert p.flags == 0x20 and p.anim_mirror == 0xFF and p.alive     # aliases are derived, not stored
    p.x += 1                                                          # real field mutation, no offsets
    assert p.x == 0x1235
    # the shipped model carries no layout knowledge: no offset constants, no bridge symbols in its namespace
    import pre2.game.model as m
    assert not hasattr(m, "DGROUP_BASE") and not hasattr(m, "PLAYER_BASE") and not hasattr(m, "PLAYER_LAYOUT")
    assert hasattr(m, "Player") and hasattr(m, "Rng")


def test_player_dataclass_round_trips_byte_exact():
    from pre2.bridge.game_layout import player_from_image, player_to_image, rng_from_image, rng_to_image
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
        player = player_from_image(st.data)
        rng = rng_from_image(st.data)
        scratch = bytearray(st.data)
        player_to_image(player, scratch)
        rng_to_image(rng, scratch)
        assert bytes(scratch) == bytes(st.data), f"game-model round-trip diverged at tick {i}"
