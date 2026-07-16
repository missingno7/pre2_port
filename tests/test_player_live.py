"""Regression guards from the REVERTED transactional-Player experiment (2026-07-16).

A live ``state.player`` was built and reverted. The lessons are permanent, so they are gated here even though
the mechanism is gone; each of these three tests corresponds to a real bug that shipped or nearly shipped.

The design that failed: ``active_player()`` re-seeded a ``Player`` from ``.data`` per pass, callers threaded it
through, then ``sync_player_to_image()`` wrote every field back. Fatal flaw: any path in the same tick that
still wrote a player field through the OFFSET contract (e.g. ``_ground_snap_or_fall``'s nested ``collision_land``,
deliberately left un-threaded because a live registration would defeat its stepped-Y override) had its write
CLOBBERED by the full-sync, because the object still held the pre-fetch value. That is the same
name-path-vs-offset-path split-brain that makes ``FieldRegistry`` unsafe for any cluster with surviving
raw-offset writers -- and a bidirectional fetch/sync design cannot fix it, only SINGLE AUTHORITY can. Player
becomes live via the Stage 2.5 boot-flip (``ObjectGraphStore.player`` as sole authority) instead.

Only ``scripts/verify_player_dataclass.py`` (5456 ticks, live-run vs object-graph) ever caught it -- not the
992-test suite, not the other seven verify scripts.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = str(ROOT / "assets")


def test_no_live_player_authority_is_registered_on_the_backend():
    """The reverted design must not creep back in un-noticed. Registering ``PlayerView`` on ``state.backend``
    (or reintroducing a ``state.player`` handle) gives the player TWO authorities -- the object and ``.data`` --
    which is exactly what produced the clobbering bug. Until the boot-flip makes ObjectGraphStore the sole
    authority, the player has exactly one home: the image."""
    from pre2.native.game_tick_demo import GameTickDemo
    from pre2.native.state import NativeGameState
    from pre2.views.dgroup_view import ByteBackend, PlayerView

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")
    gtd = GameTickDemo.load(demo)
    state = NativeGameState(bytearray(gtd.seed))
    assert isinstance(state.backend, ByteBackend)
    assert not hasattr(state, "player"), (
        "a live state.player handle is back -- it was reverted because its full-sync clobbered offset-path "
        "writes in the same tick; make the player live via the boot-flip (single authority) instead")
    reg = state.backend._registry
    assert reg is None or PlayerView not in reg._by_cls, (
        "PlayerView must not be registered on state.backend: direct PlayerView(state) constructions on "
        "one-shot event paths (game-over reset, cave-teleport, attract-title) write a player field and read "
        "'.data' back immediately, with no sync point of their own")


def test_one_shot_event_paths_see_their_player_write_in_data_immediately():
    """The concrete failure the above prevents: the game-over reset's death-pose write must land in ``.data``
    at once. Under the reverted global registration it vanished into an object nothing later flushed, and this
    exact assertion (a raw byte read right after the call) came back stale."""
    import pytest
    from pre2.gaps import Pre2GameOverTransition
    from pre2.native.state import NativeGameState
    import pre2.native.level_state as ls

    DGROUP_BASE = 0x1A0F << 4

    def _skip_bounce(st):
        return iter(())

    orig = ls.native_death_bounce_509d
    ls.native_death_bounce_509d = _skip_bounce
    try:
        d = bytearray(0x100000)
        d[DGROUP_BASE + 0x2879] = 1     # a real death is pending
        d[DGROUP_BASE + 0x2D8A] = 6     # was on level 7 (index 6)
        state = NativeGameState(d)
        with pytest.raises(Pre2GameOverTransition):
            for _ in ls.native_4f6c(state):
                pass
        assert state.data[DGROUP_BASE + 0x4F20] == 0x0D, (
            "the death-pose byte must be visible in .data immediately -- no sync point exists on this path")
    finally:
        ls.native_death_bounce_509d = orig


def test_verify_native_reproduces_the_vm_oracle_digest():
    """Kept from the RNG slice: a full demo replayed on native still reproduces the VM's recorded gameplay
    digest every tick, via the SAME verify_native() scripts/verify_native_tick_demo.py drives. RNG remains
    genuinely live on state.rng (it has no surviving raw-offset writers, which is exactly why persistent
    registration is safe for it and was not for Player)."""
    from pre2.native.game_tick_demo import GameTickDemo, verify_native

    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    matched, divergence = verify_native(gtd, game_root=ASSETS)
    assert divergence is None, f"native diverged from the VM oracle after {matched} ticks: {divergence}"
    assert matched == len(gtd.keys)
