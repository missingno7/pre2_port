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
