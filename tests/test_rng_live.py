"""The RNG live-wiring proof: state.rng (pre2.game.model.Rng) is the shipped runtime's real source of truth
for the LCG/rotate generator, not a parallel copy that silently forks from the byte image.

This is the standing regression guard for the whole native-dataclass-lift RNG slice: it exercises the SAME
verify_native tick-by-tick digest check the standalone verify_native_tick_demo.py script runs, but as a
permanent pytest test — verify_native() itself had NO pytest coverage before this, which is exactly how the
sign-extension bug and (separately) the digest-staleness bug in this slice's development both went unnoticed
until the standalone scripts were run by hand."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = str(ROOT / "assets")


def test_state_rng_is_genuinely_live_across_a_real_playthrough():
    """Driving real gameplay ticks must actually mutate state.rng (not silently no-op) — the mechanism proof
    that's easy to lose if a future refactor re-routes RNG reads/writes around the live object."""
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    state = NativeGameState(bytearray(gtd.seed))
    before = (state.rng.lcg_a, state.rng.lcg_b, state.rng.lcg_c, state.rng.lcg_d, state.rng.ror)
    for i in range(60):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(state)
    after = (state.rng.lcg_a, state.rng.lcg_b, state.rng.lcg_c, state.rng.lcg_d, state.rng.ror)
    assert after != before, "state.rng never changed over 60 real ticks -- RNG rolls are not landing on it"


def test_verify_native_reproduces_the_vm_oracle_digest_with_rng_live():
    """The real proof: a full demo replayed on native (RNG running live off state.rng the whole time) still
    reproduces the VM's recorded gameplay digest every tick -- the wiring didn't change observable behaviour,
    it only moved where the RNG bytes physically live. Uses the SAME verify_native() the standalone
    scripts/verify_native_tick_demo.py drives (default demo, the one that actually caught this slice's
    digest-staleness regression during development), so a regression here is caught by `pytest`, not only by
    running that script by hand."""
    from pre2.native.game_tick_demo import GameTickDemo, verify_native

    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    matched, divergence = verify_native(gtd, game_root=ASSETS)
    assert divergence is None, f"native diverged from the VM oracle after {matched} ticks: {divergence}"
    assert matched == len(gtd.keys)
