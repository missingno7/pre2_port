"""Gate: the REAL interactive frame driver (native_frame_step_tagged, the generator play_native pumps every
frame) yields byte-identical frames with the gameplay state of record on the offset-free object graph
(object_store=True) vs the shipped byte-image default. scripts/verify_object_playloop.py is the multi-demo
proof; this runs one representative demo (with a mid-run transition) as a fast regression gate.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_real_product_loop_runs_identically_on_the_object_graph():
    demo = ROOT / "artifacts" / "demo_pre2_20260708_014331"      # a short run through a respawn transition
    if not (demo / "game_tick_demo.bin").exists():
        import pytest
        pytest.skip("demo corpus not present")
    from verify_object_playloop import main as verify_main
    old_argv = sys.argv
    sys.argv = ["verify_object_playloop.py", str(demo)]
    try:
        rc = verify_main()
    finally:
        sys.argv = old_argv
    assert rc == 0, "the real product frame loop diverged on the object graph"


def test_the_products_DEFAULT_frame_loop_matches_the_byte_image_reference():
    """The Stage 2.5 boot-flip's regression guard, and a real coverage gap closed (2026-07-16).

    Everything else pins the loop with an EXPLICIT ``store=`` -- ``verify_object_playloop`` passes
    ``ObjectStore()`` for the object run and ``None`` for the reference; no test anywhere called
    ``native_frame_step_tagged`` the way the PRODUCT does, i.e. taking the default. Measured: instrumenting a
    flipped default and running the whole suite showed the flipped path taken ZERO times while all 991 tests
    passed -- so pytest could not have caught a broken flip at all.

    This locks the invariant the flip must preserve, independent of what the default happens to BE: the frames
    the product's default path yields == the frames the known-good byte-image path yields. Today (default
    ``None``) that is the byte image and this is cheap; the moment Phase D changes the default to the object
    graph, this becomes the assertion that the flip is byte-exact through the real driver."""
    demo = ROOT / "artifacts" / "demo_pre2_20260708_014331"      # a short run through a respawn transition
    if not (demo / "game_tick_demo.bin").exists():
        import pytest
        pytest.skip("demo corpus not present")

    from pre2.gaps import Pre2HybridGap
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.runtime import native_frame_step_tagged
    from pre2.native.state import NativeGameState
    from pre2.native.vga import NativeVGA

    gtd = GameTickDemo.load(demo / "game_tick_demo.bin")
    n = min(120, gtd.n_ticks)
    gr = str(ROOT / "assets")

    def drive(**kw):
        """kw is either {} (the PRODUCT's default) or {'store': None} (the explicit byte-image reference)."""
        st, dos, out = NativeGameState(bytearray(gtd.seed)), NativeVGA(), []
        for i in range(n):
            _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            try:
                for planes, page, interp, tx in native_frame_step_tagged(st, dos, 0, game_root=gr, **kw):
                    out.append((b"".join(bytes(p) for p in planes) if planes else b"", page, interp, tx))
            except Pre2HybridGap as e:
                out.append(("GAP", type(e).__name__))
                break
        return out

    default_path = drive()                  # exactly how play_native calls it
    reference = drive(store=None)           # the known-good byte-image path
    assert default_path, "the driver yielded no frames -- the demo did not run"
    assert default_path == reference, (
        "the product's DEFAULT frame loop diverged from the byte-image reference: whatever store the default "
        "selects must yield byte-identical frames through the real driver")
