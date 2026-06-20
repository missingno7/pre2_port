"""Regenerate docs/pre2/recovered_islands.md from the in-code @oracle_link metadata.

The manifest is generated, never hand-edited: code is the source of truth. Run after
adding/annotating a recovered island; tests/test_island_registry.py checks it is in sync.

    python scripts/gen_island_manifest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pre2.islands import MANIFEST_PATH, collect_islands, render_manifest  # noqa: E402

if __name__ == "__main__":
    MANIFEST_PATH.write_text(render_manifest(), encoding="utf-8")
    print(f"wrote {MANIFEST_PATH} ({len(collect_islands())} islands)")
