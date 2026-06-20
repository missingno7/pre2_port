"""OracleLink provenance is well-formed and present on the recovered functions.

These links are an optional testing/documentation aid (see pre2/recovered/oracle.py);
this guards that the metadata stays valid without affecting the functions themselves.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.frame_renderer import draw_tile_row  # noqa: E402
from pre2.recovered.oracle import STATUSES, OracleLink, oracle_link  # noqa: E402
from pre2.recovered.renderer import blit_sprite  # noqa: E402


def test_decorator_attaches_link_without_changing_callable():
    @oracle_link("1030:1234", "some contract", "RECOVERED")
    def f(a, b):
        return a + b

    assert f(2, 3) == 5  # behaviour unchanged
    assert isinstance(f.oracle_link, OracleLink)
    assert f.oracle_link.boundary == "1030:1234"


def test_invalid_status_rejected():
    try:
        OracleLink("1030:0000", "x", "BOGUS")
    except ValueError:
        return
    raise AssertionError("expected ValueError for bad status")


def test_recovered_functions_carry_verified_links():
    for fn, boundary in ((blit_sprite, "1030:3B69"), (draw_tile_row, "1030:346E")):
        link = fn.oracle_link
        assert link.boundary == boundary
        assert link.status in STATUSES
        assert link.status == "VERIFIED"
        assert link.contract  # non-empty description


def test_composition_matches_asm_callgraph():
    # draw_tile_row (346E) composes blit_sprite (3B69); the ASM 346E calls 3B69.
    assert draw_tile_row.oracle_link.boundary == "1030:346E"
    assert blit_sprite.oracle_link.boundary == "1030:3B69"
