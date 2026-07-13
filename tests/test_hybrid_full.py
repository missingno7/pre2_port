"""Phase 5 proof (fast slice): the WHOLE product path (boot -> gameplay -> render) runs gameplay on the
named field store, byte-identical to the ByteBackend reference. The deeper run lives in
scripts/verify_hybrid_full.py; this gates a short boot+play+render so a seam-routing regression fails CI."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

_HAS_ASSETS = (ROOT / "assets" / "PRE0.SQZ").exists() or bool(list((ROOT / "assets").glob("*.SQZ")))


@pytest.mark.skipif(not _HAS_ASSETS, reason="game assets not present")
def test_hybrid_full_boot_play_render():
    from verify_hybrid_full import run
    ok, msg = run(30)
    assert ok, f"product did not run gameplay on the field store: {msg}"
