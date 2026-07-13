"""Gate: the REAL product render loop (a gameplay tick + full native_render EVERY frame) emits byte-identical
VGA planes with the gameplay state of record on the offset-free object graph. scripts/verify_object_render.py
is the multi-demo proof; this runs one representative demo as a fast regression gate.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_every_frame_render_is_byte_identical_on_the_object_graph():
    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423"
    if not (demo / "game_tick_demo.bin").exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    from verify_object_render import main as verify_main
    old_argv = sys.argv
    sys.argv = ["verify_object_render.py", str(demo), "300"]
    try:
        rc = verify_main()
    finally:
        sys.argv = old_argv
    assert rc == 0, "the every-frame render loop diverged on the object graph"
