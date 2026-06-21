"""The recovered-island registry is self-describing and the manifest cannot drift.

Code is the source of truth: every island's metadata is the @oracle_link on its
function, auto-discovered by pre2.islands. These tests check the metadata is
well-formed and that docs/pre2/recovered_islands.md matches what the code declares
(regenerate with `python scripts/gen_island_manifest.py`).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.islands import (  # noqa: E402
    MANIFEST_PATH, STATUSES, collect_islands, render_manifest,
)

_BOUNDARY = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}$")


def test_islands_are_discovered():
    islands = collect_islands()
    boundaries = {link.boundary for _m, _n, link in islands}
    # the currently recovered+annotated islands
    assert {"1030:107B", "1030:348D", "1030:35A1", "1030:3B88", "1030:4316"} <= boundaries


def test_every_island_metadata_is_well_formed():
    for modname, name, link in collect_islands():
        where = f"{modname}.{name}"
        assert _BOUNDARY.match(link.boundary), f"{where}: bad boundary {link.boundary!r}"
        assert link.status in STATUSES, f"{where}: bad status {link.status!r}"
        assert link.contract.strip(), f"{where}: empty contract"
        assert link.merge_target.strip(), f"{where}: missing merge_target"


def test_manifest_matches_code():
    generated = render_manifest()
    committed = MANIFEST_PATH.read_text(encoding="utf-8")
    assert generated == committed, (
        "docs/pre2/recovered_islands.md is stale — regenerate with "
        "`python scripts/gen_island_manifest.py`"
    )
