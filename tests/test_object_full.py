"""Gate: the product's boot -> gameplay -> render path runs gameplay on the object graph (gap #2)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_product_path_runs_gameplay_on_the_object_graph():
    if not (ROOT / "assets").exists():
        import pytest
        pytest.skip("game assets not present")
    from verify_object_full import run
    ok, msg = run(40)
    assert ok, msg
