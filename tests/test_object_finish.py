"""Gate: the WHOLE game lifecycle (transitions included: level-end, respawn, cave-teleport) runs on the
offset-free object graph, byte-exact vs the recorded VM digest. scripts/verify_object_finish.py is the
full-corpus proof; this runs a couple of representative demos as a fast regression gate.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _run(demo_rel):
    from verify_object_finish import main as verify_main
    demo = ROOT / demo_rel
    if not (demo / "game_tick_demo.bin").exists():
        import pytest
        pytest.skip(f"{demo_rel} corpus not present")
    old_argv = sys.argv
    sys.argv = ["verify_object_finish.py", str(demo)]
    try:
        rc = verify_main()
    finally:
        sys.argv = old_argv
    assert rc == 0, f"{demo_rel} did not reproduce the whole lifecycle on the object graph"


def test_finish_demo_reproduces_the_whole_lifecycle_on_the_object_graph():
    """Level-end (8->9) + the entire level-9 boss fight (mode-9 boss script), 1579 ticks."""
    _run("artifacts/demo_pre2_finish_game_norepl_20260703_165400")


def test_cave_teleport_demo_reproduces_the_whole_lifecycle_on_the_object_graph():
    """A cave-teleport transition mid-run, 1729 ticks."""
    _run("artifacts/demo_pre2_20260705_003258")
