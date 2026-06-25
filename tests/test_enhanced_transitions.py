"""Enhanced transition projections — geometry tests (the byte-exact-vs-faithful equivalence is proven by
``pre2/probes/verify_enhanced_vfade.py`` against compose_vfade_planes; these cover the pure RGB geometry)."""
from __future__ import annotations

import numpy as np

from pre2.enhanced.transition_controller import EnhancedTransition
from pre2.enhanced.transitions import VIEWPORT_H, apply_curtain, apply_iris, apply_vfade


def test_apply_vfade_blacks_converging_bands():
    f = np.full((200, 320, 3), 200, np.uint8)
    apply_vfade(f, 40, 120)
    assert (f[:40] == 0).all(), "top band not blacked"
    assert (f[120:176] == 0).all(), "bottom band not blacked"
    assert (f[40:120] == 200).all(), "middle (uncleared) must be untouched"
    assert (f[176:] == 200).all(), "HUD band must be untouched (vfade is viewport-only)"


def test_apply_vfade_inactive_is_noop():
    f = np.full((200, 320, 3), 123, np.uint8)
    apply_vfade(f, 0, 176)             # top=0, bot=176 -> nothing cleared == a normal frame
    assert (f == 123).all()


def test_apply_vfade_fully_closed_blacks_whole_viewport():
    f = np.full((200, 320, 3), 200, np.uint8)
    apply_vfade(f, 88, 88)             # bands meet in the middle
    assert (f[:176] == 0).all()
    assert (f[176:] == 200).all()


def test_apply_vfade_clamps_out_of_range():
    f = np.full((200, 320, 3), 50, np.uint8)
    apply_vfade(f, -5, 999)            # clamps to [0,176]; bot>=176 -> no bottom band, top<=0 -> no top band
    assert (f == 50).all()


def test_apply_iris_keeps_inside_blacks_outside():
    f = np.full((200, 320, 3), 200, np.uint8)
    apply_iris(f, 50, 160, 100)        # circle radius 50 about screen (col 160, row 100)
    assert tuple(f[100, 160]) == (200, 200, 200), "centre must stay fully visible"
    assert tuple(f[100, 120]) == (200, 200, 200), "well inside (40px) stays visible"
    assert tuple(f[100, 230]) == (0, 0, 0), "well outside (70px) must be black"
    assert tuple(f[10, 10]) == (0, 0, 0), "far corner must be black"


def test_apply_iris_radius_zero_is_fully_black():
    f = np.full((200, 320, 3), 200, np.uint8)
    apply_iris(f, 0, 160, 100)
    assert (f == 0).all()


def test_apply_iris_radius_follows_state():
    # a larger radius keeps a point visible that a smaller radius blacks -> the mask tracks the radius phase.
    p = (100, 210)   # 50px right of centre col 160, row 100
    small = np.full((200, 320, 3), 200, np.uint8); apply_iris(small, 30, 160, 100)
    big = np.full((200, 320, 3), 200, np.uint8); apply_iris(big, 80, 160, 100)
    assert tuple(small[p]) == (0, 0, 0), "outside the small radius -> black"
    assert tuple(big[p]) == (200, 200, 200), "inside the larger radius -> visible"


def test_iris_transition_is_present_time_driven():
    # The controller closes from WALL-CLOCK progress (not source samples): later wall time -> smaller circle.
    old = np.full((200, 320, 3), 200, np.uint8)
    tr = EnhancedTransition("iris", 100.0, old_frame=old, center=(160, 100), sprites=[])
    v0 = int(np.any(tr.render(100.0) != 0, axis=2).sum())
    v_mid = int(np.any(tr.render(100.8) != 0, axis=2).sum())
    v_late = int(np.any(tr.render(101.5) != 0, axis=2).sum())
    assert v0 > v_mid > v_late, "iris must close as present time advances"


def test_iris_transition_covered_is_black_then_releases():
    old = np.full((200, 320, 3), 200, np.uint8)
    tr = EnhancedTransition("iris", 100.0, old_frame=old, center=(160, 100), sprites=[])
    tr.note_ended(101.7)                                  # recovered effect ended -> covered
    assert (tr.render(101.8) == 0).all(), "covered phase must be black (not the old/new frame -> no flash)"
    assert not tr.released(101.8, scene_ready=False), "must hold black briefly, not release instantly"
    assert tr.released(101.8, scene_ready=True), "release immediately once the new scene is ready"
    assert tr.released(102.5, scene_ready=False), "release after the bounded covered-black hold"


def test_apply_curtain_reveals_center_out():
    new = np.full((200, 320, 3), 200, np.uint8)
    assert (apply_curtain(np.zeros((200, 320, 3), np.uint8), new, 0.0) == 0).all(), "progress 0 -> black"
    f = apply_curtain(np.zeros((200, 320, 3), np.uint8), new, 0.5)   # continuous half-width centre band
    assert tuple(f[88, 160]) == (200, 200, 200), "centre revealed first"
    assert tuple(f[88, 8]) == (0, 0, 0), "edges still black at half progress"
    f = apply_curtain(np.zeros((200, 320, 3), np.uint8), new, 1.0)
    assert tuple(f[88, 0]) == (200, 200, 200) and tuple(f[88, 319]) == (200, 200, 200), "progress 1 -> full width"
    assert tuple(f[180, 160]) == (0, 0, 0), "HUD rows stay black"


def test_apply_curtain_is_monotonic_centre_out():
    new = np.full((200, 320, 3), 200, np.uint8)
    widths = [int((apply_curtain(np.zeros((200, 320, 3), np.uint8), new, p)[88] != 0).any(axis=1).sum())
              for p in (0.2, 0.4, 0.6, 0.8, 1.0)]
    assert widths == sorted(widths) and widths[0] > 0 and widths[-1] == 320, "reveal grows monotonically to full"


def test_room_transition_covered_viewport_black_hud_kept_no_blink():
    # The blink bug: between close and open the new room is already loaded (fresh), but the COVERED viewport
    # must stay black. The HUD strip is intentionally KEPT visible (frozen at the old room) through the whole
    # transition -- the faithful curtain overlays the held HUD too.
    old = np.full((200, 320, 3), 200, np.uint8)
    tr = EnhancedTransition("room", 100.0, old_frame=old)
    tr.note_ended(100.5)                                  # vfade ended, no curtain yet -> covered
    assert tr.phase == "covered"
    out = tr.render(100.6)
    assert (out[:VIEWPORT_H] == 0).all(), "covered viewport must be black (never the fresh new room -> no blink)"
    assert (out[VIEWPORT_H:] == 200).all(), "the HUD strip stays visible (frozen) through the transition"


def test_room_transition_open_reveals_new_frame():
    old = np.zeros((200, 320, 3), np.uint8)
    new = np.full((200, 320, 3), 150, np.uint8)
    tr = EnhancedTransition("room", 100.0, old_frame=old)
    tr.note_curtain(100.5, completed=10, new_frame=new)   # curtain -> open
    assert tr.phase == "open"
    out = tr.render(100.5 + 1.0)                            # well past the open duration -> fully revealed
    assert tuple(out[88, 160]) == (150, 150, 150), "open phase reveals the captured new room"
