"""Phase 4 proof (fast slice): the gameplay tick runs with named mutable state in a FieldBackend, off the
byte image, byte-identical to the reference. The full corpus lives in scripts/verify_hybrid_tick.py; this
gates one demo so a regression in the seam routing fails the normal test run."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

_DEMO = ROOT / "artifacts" / "demo_cold_20260712_172030"      # the short cold-start demo (15 ticks)


@pytest.mark.skipif(not (_DEMO / "game_tick_demo.bin").exists(), reason="demo tick bin not present")
def test_hybrid_tick_cold_start_full():
    from verify_hybrid_tick import run_demo
    n, diverge = run_demo(_DEMO)
    assert diverge is None, (
        f"hybrid diverged after {n} ticks at 0x{diverge[0]:04X} ({diverge[3]}) — a native module accesses "
        f"a named offset via raw .data instead of the state.backend seam")
    assert n >= 15
