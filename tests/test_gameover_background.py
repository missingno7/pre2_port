"""Tests for the recovered game-over diorama background (pre2.recovered.gameover_background).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_gameover_full.py: the background
composed from the decoded GAMEOVER.SQZ reproduces the displayed viewport Δ=0, no VM framebuffer). These
lock the staging layout (top 200 rows black, bottom 200 = the asset) and the de-interleave."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.gameover_background import (  # noqa: E402
    build_staging, deinterleave_asset, render_gameover_background)

_PLANE = 0x1F40


def _asset(byte=0x99):
    # plane-major: plane p filled with (byte + p)
    return b"".join(bytes([(byte + p) & 0xFF]) * _PLANE for p in range(4))


def test_deinterleave_splits_four_planes():
    planes = deinterleave_asset(_asset(0x10))
    assert len(planes) == 4
    for p in range(4):
        assert len(planes[p]) == _PLANE
        assert planes[p][0] == 0x10 + p and planes[p][-1] == 0x10 + p


def test_staging_top_black_bottom_asset():
    staging = build_staging(deinterleave_asset(_asset(0x40)))
    for p in range(4):
        # top 200 rows (0 .. 0x1F40) are black
        assert all(b == 0 for b in staging[p][:_PLANE])
        # bottom 200 rows (0x1F40 .. 0x3E80) are the asset plane
        assert all(b == (0x40 + p) & 0xFF for b in staging[p][_PLANE:2 * _PLANE])


def test_render_window_picks_diorama_at_scroll():
    # at scroll 200 the window starts exactly at the diorama (staging row 200 = asset row 0)
    planes = render_gameover_background(_asset(0x55), scroll=200, page=0)
    for p in range(4):
        assert planes[p][0] == (0x55 + p) & 0xFF          # first window byte = asset start


def test_render_sky_above_diorama():
    # at scroll 100 the window top (rows 100..200) is the black sky, then the diorama
    planes = render_gameover_background(_asset(0x33), scroll=100, page=0x2000)
    # the first 100 rows of the window are sky (black)
    assert all(planes[0][(0x2000 + o) & 0xFFFF] == 0 for o in range(100 * 0x28))
    # row 100 of the window = staging row 200 = the diorama
    assert planes[0][(0x2000 + 100 * 0x28) & 0xFFFF] == 0x33


def test_asset_too_small_raises():
    import pytest
    with pytest.raises(ValueError):
        deinterleave_asset(b"\x00" * 100)
