"""Tests for the pure SceneCompositor (pre2.recovered.scene_compositor).

The byte-exact overlay proof is the live probe (pre2/probes/verify_gameover_scene.py: the recovered
object overlay composes Δ=0 over a diagnostic fixture background). These lock the layer semantics: the
explicit gap marker (NOT the VM frame), the fixture/recovered passthrough, the overlay application order,
and the status each background kind reports."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.scene_compositor import (  # noqa: E402
    FixtureBackground, MissingBackgroundGap, RecoveredBackground, SceneStatus, compose_scene)


def _planes_bytes(value=0x11):
    return tuple(bytes([value]) * 0x10000 for _ in range(4))


def test_missing_background_is_a_gap_not_blank_not_vm():
    planes, status = compose_scene(MissingBackgroundGap("diorama"), [], page=0)
    assert status == SceneStatus.BACKGROUND_GAP
    # the gap is an EXPLICIT marker: some viewport pixels are set (not all-zero / not a copied frame)
    nonzero = sum(1 for p in range(4) for o in range(200 * 0x28) if planes[p][o])
    assert nonzero > 0


def test_fixture_background_passthrough_and_status():
    fix = _planes_bytes(0x3C)
    planes, status = compose_scene(FixtureBackground(fix, "oracle"), [], page=0)
    assert status == SceneStatus.FIXTURE
    assert planes[0][:4] == bytearray(b"\x3c\x3c\x3c\x3c")


def test_recovered_background_status_complete():
    planes, status = compose_scene(RecoveredBackground(_planes_bytes(0x07)), [], page=0)
    assert status == SceneStatus.COMPLETE
    assert planes[2][100] == 0x07


def test_overlays_applied_in_order_over_background():
    calls = []

    def ov_a(planes, page):
        calls.append("a")
        planes[0][page] = 0xF0

    def ov_b(planes, page):
        calls.append("b")
        planes[0][page] |= 0x0F          # sees ov_a's write -> 0xFF

    planes, status = compose_scene(RecoveredBackground(_planes_bytes(0x00)), [ov_a, ov_b], page=0x10)
    assert calls == ["a", "b"]           # ordered
    assert planes[0][0x10] == 0xFF       # ov_b composited over ov_a over the background


def test_gap_then_overlay_overlay_wins_on_top():
    # the recovered overlay draws over the gap marker (the marker never hides recovered content)
    def ov(planes, page):
        for o in range(0x28):
            planes[1][o] = 0x99

    planes, _ = compose_scene(MissingBackgroundGap("bg"), [ov], page=0)
    assert all(planes[1][o] == 0x99 for o in range(0x28))
