"""The Player live-wiring proof: state.player (pre2.game.model.Player) is the shipped runtime's real handle
for the player-struct fields DURING the player-owning passes (player_fsm_step/collision/player_flying_484e/
tick_terrain_entities), not a parallel copy that silently forks from the byte image.

Player's live-wiring is DELIBERATELY narrower than RNG's: nothing is registered globally on state.backend.
Two real bugs surfaced during development, both now permanent regression guards here:

1. A first attempt DID register PlayerView on state.backend globally (mirroring RNG). That made every direct
   PlayerView(state) construction ANYWHERE resolve live -- including one-shot event paths (level_state.py's
   game-over/respawn reset, cave-teleport, attract-title, cold-boot) that write a player field and expect it
   in .data IMMEDIATELY, with no sync point of their own. 14 pytest failures caught this before it shipped.
2. The object-graph backend's own Player.anim_ptr is a bridge-swizzled AssetCursor/RawRef, not a plain int --
   registering it through this registry crashed the instant any recovered function touched p.anim_ptr
   (TypeError: unsupported operand type(s) for &: 'RawRef' and 'int').

The fix: active_player() is TRANSACTIONAL (re-seeds state.player fresh from .data on every call, returns None
for anything but the default ByteBackend) and nothing is ever registered on state.backend itself. Callers
fetch it once, thread the same reference through their whole pass, then sync_player_to_image() immediately."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = str(ROOT / "assets")


def test_state_player_is_genuinely_live_across_a_real_playthrough():
    """Driving real gameplay ticks must actually mutate state.player (not silently no-op) -- the mechanism
    proof that's easy to lose if a future refactor re-routes player-struct reads/writes around the live
    object."""
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    state = NativeGameState(bytearray(gtd.seed))
    before = (state.player.x, state.player.y, state.player.xvel, state.player.yvel, state.player.sprite)
    for i in range(60):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(state)
    after = (state.player.x, state.player.y, state.player.xvel, state.player.yvel, state.player.sprite)
    assert after != before, "state.player never changed over 60 real ticks -- player writes are not landing on it"


def test_verify_native_reproduces_the_vm_oracle_digest_with_player_live():
    """The real proof: a full demo replayed on native (the player-struct fields running live off state.player
    for the whole player-update pass, every tick) still reproduces the VM's recorded gameplay digest every
    tick -- the wiring didn't change observable behaviour, it only moved where those bytes physically live
    DURING the pass that owns them. Uses the SAME verify_native() the standalone
    scripts/verify_native_tick_demo.py drives, so a regression here is caught by pytest, not only by running
    that script by hand."""
    from pre2.native.game_tick_demo import GameTickDemo, verify_native

    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    matched, divergence = verify_native(gtd, game_root=ASSETS)
    assert divergence is None, f"native diverged from the VM oracle after {matched} ticks: {divergence}"
    assert matched == len(gtd.keys)


def test_one_shot_event_paths_are_not_silently_made_live():
    """Regression guard for bug #1 above: a one-shot event path (here, the game-over reset's death-pose write)
    that constructs its OWN PlayerView(state) and writes a player-struct field must land in .data IMMEDIATELY
    -- nothing may be registered on state.backend that would silently redirect this write onto an object
    nothing later flushes. If this ever regresses, the write vanishes and this assertion catches it exactly
    the way the original bug was caught: a raw byte read right after the call comes back stale."""
    import pytest
    from pre2.gaps import Pre2GameOverTransition
    from pre2.native.state import NativeGameState
    from pre2.views.dgroup_view import ByteBackend, PlayerView
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
        # RngView IS legitimately registered globally (RNG has no field-representation mismatch across
        # backends) -- the invariant under test is narrower: PlayerView specifically must never be, since
        # doing so once made every direct PlayerView(state) construction (like this test's own scenario)
        # resolve live with no corresponding sync point.
        reg = state.backend._registry
        assert isinstance(state.backend, ByteBackend)
        assert reg is None or PlayerView not in reg._by_cls, (
            "PlayerView must never be registered on state.backend -- Player live-wiring is transactional")
        with pytest.raises(Pre2GameOverTransition):
            for _ in ls.native_4f6c(state):
                pass
        assert state.data[DGROUP_BASE + 0x4F20] == 0x0D, (
            "the death-pose byte must be visible in .data immediately -- no sync point exists on this path")
    finally:
        ls.native_death_bounce_509d = orig


def test_active_player_returns_none_on_the_object_graph_backend():
    """Regression guard for bug #2 above: the object-graph backend's own Player.anim_ptr is a bridge-swizzled
    AssetCursor/RawRef, not a plain int. active_player() must return None there (never the swizzled object) --
    registering/reading it through this module's plain-int-assuming registry crashes on first touch."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.game.model import Player
    from pre2.game.ref import RawRef
    from pre2.native.game_tick_demo import GameTickDemo
    from pre2.native.state import NativeGameState

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")
    gtd = GameTickDemo.load(demo)
    state = NativeGameState(bytearray(gtd.seed))
    state.backend = DataclassBackend(state)
    assert isinstance(state.backend.player, Player)
    assert isinstance(state.backend.player.anim_ptr, RawRef), (
        "test assumption stale -- the object-graph Player's anim_ptr is no longer swizzled, "
        "active_player()'s ByteBackend-only guard may be safe to relax")
    assert state.active_player() is None
