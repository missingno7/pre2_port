"""Unit tests for the pure virtual-touch resolver (pre2.native.touch) — no pygame, no device.

Drives TouchControls with synthetic finger dicts against a fixed landscape window and asserts the five
movement flags + the jump edge. The layout fractions live in pre2.native.touch; these tests derive the
probe points from the same Layout so they track any retuning.
"""
from __future__ import annotations

from pre2.native import touch
from pre2.native.touch import TouchControls, layout_for

SIZE = (1280, 720)                       # a typical landscape phone-ish window
LAY = layout_for(SIZE)


def _drag(base, dx, dy):
    return (base[0] + dx, base[1] + dy)


def test_no_fingers_is_neutral():
    tc = TouchControls()
    flags, rm, edge = tc.update({}, SIZE)
    assert not flags.any()
    assert not edge
    assert not rm.stick_active
    assert rm.stick_base == LAY.stick_rest


def test_stick_right_and_left():
    tc = TouchControls()
    base = (LAY.stick_rest[0], LAY.stick_rest[1])
    # plant the stick at rest, then push full-right
    tc.update({1: base}, SIZE)
    flags, rm, _ = tc.update({1: _drag(base, LAY.stick_radius, 0)}, SIZE)
    assert flags.right and not flags.left and not flags.up and not flags.down
    assert rm.stick_active
    # same finger swings full-left
    flags, _, _ = tc.update({1: _drag(base, -LAY.stick_radius, 0)}, SIZE)
    assert flags.left and not flags.right


def test_stick_deadzone_is_neutral():
    tc = TouchControls()
    base = (LAY.stick_rest[0], LAY.stick_rest[1])
    tc.update({1: base}, SIZE)
    tiny = LAY.stick_radius * (touch.TouchControls.DEADZONE * 0.5)
    flags, _, _ = tc.update({1: _drag(base, tiny, 0)}, SIZE)
    assert not flags.any()


def test_stick_diagonal_down_right():
    tc = TouchControls()
    base = (LAY.stick_rest[0], LAY.stick_rest[1])
    tc.update({1: base}, SIZE)
    d = LAY.stick_radius
    flags, _, _ = tc.update({1: _drag(base, d, d)}, SIZE)   # clamped to ring, both axes past threshold
    assert flags.right and flags.down and not flags.left and not flags.up


def test_stick_up_only_with_bash():
    # the stick emits UP only WHILE BASH is held (an upward bash); up-alone never registers, so walking
    # up-diagonal can't trigger a stray jump.
    tc = TouchControls()
    base = (LAY.stick_rest[0], LAY.stick_rest[1])
    tc.update({1: base}, SIZE)
    flags, rm, _ = tc.update({1: _drag(base, 0, -LAY.stick_radius)}, SIZE)   # stick up, no bash
    assert not flags.up
    assert rm.knob[1] < rm.stick_base[1]                                     # full circle: the knob DOES rise
    # stick up + BASH held -> up + fire (bash upward)
    tc2 = TouchControls()
    tc2.update({1: base}, SIZE)
    flags, _, _ = tc2.update({1: _drag(base, 0, -LAY.stick_radius), 2: LAY.bash_center}, SIZE)
    assert flags.up and flags.fire


def test_jump_button_sets_up_with_edge_once():
    tc = TouchControls()
    flags, _, edge = tc.update({7: LAY.jump_center}, SIZE)
    assert flags.up and not flags.fire
    assert edge                                   # first frame down -> edge
    flags, _, edge = tc.update({7: LAY.jump_center}, SIZE)
    assert flags.up and not edge                  # still held -> no new edge
    flags, _, edge = tc.update({}, SIZE)          # lifted
    assert not flags.up and not edge


def test_bash_button_sets_fire():
    tc = TouchControls()
    flags, rm, _ = tc.update({3: LAY.bash_center}, SIZE)
    assert flags.fire and not flags.up
    assert rm.bash_pressed


def test_three_fingers_stick_jump_bash_together():
    tc = TouchControls()
    base = (LAY.stick_rest[0], LAY.stick_rest[1])
    tc.update({1: base}, SIZE)
    fingers = {
        1: _drag(base, -LAY.stick_radius, 0),     # stick full-left
        2: LAY.jump_center,                       # JUMP
        3: LAY.bash_center,                        # BASH
    }
    flags, rm, _ = tc.update(fingers, SIZE)
    assert flags.left and flags.up and flags.fire
    assert not flags.right and not flags.down
    assert rm.jump_pressed and rm.bash_pressed and rm.stick_active


def test_button_finger_held_after_sliding_off():
    tc = TouchControls()
    tc.update({5: LAY.bash_center}, SIZE)         # claim BASH
    off = (LAY.bash_center[0] + LAY.button_radius * 4, LAY.bash_center[1])
    flags, _, _ = tc.update({5: off}, SIZE)       # same finger drifts far off the button
    assert flags.fire                             # ownership retained until it lifts


def test_menu_tap_is_fire():
    # front-end gestures (event-driven): a TAP (down then up, no swipe) accumulates one fire pulse.
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.on_down(1, (600, 360))
    assert g.poll() == (False, 0)                            # nothing until it completes
    g.on_up(1)
    assert g.poll() == (True, 0)                             # the tap fires
    assert g.poll() == (False, 0)                            # drained (fire is a one-poll pulse)


def test_menu_fast_tap_same_poll_still_fires():
    # THE REGRESSION: a fast tap delivers down+up before a single poll (real devices do this constantly). The
    # event-driven recogniser must still fire — a poll-of-the-current-finger-set recogniser would miss it.
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.on_down(1, (600, 360))
    g.on_up(1)                                               # up in the SAME poll as down
    assert g.poll() == (True, 0)


def test_menu_swipe_down_is_down_arrow_and_never_fires():
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.on_down(1, (600, 280))
    g.on_move(1, (600, 280 + 60), SIZE)                     # drag down 60px (> 0.06*720 = 43px threshold)
    assert g.poll() == (False, touch.SCAN_DOWN)
    g.on_up(1)                                               # lift -> a swipe must NOT also fire
    assert g.poll() == (False, 0)


def test_menu_swipe_up_is_up_arrow():
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.on_down(1, (600, 400))
    g.on_move(1, (600, 400 - 60), SIZE)                     # drag up past the threshold
    assert g.poll() == (False, touch.SCAN_UP)


def test_menu_swipe_disabled_makes_drag_a_tap():
    # OLDIES / intro / main-menu / carte are tap-only: with swipe_enabled False a vertical drag is NOT a swipe,
    # so it emits no arrow and the lift still FIRES (a stray drag must not swallow the tap).
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.swipe_enabled = False
    g.on_down(1, (600, 280))
    g.on_move(1, (600, 280 + 120), SIZE)                   # a big drag — would be a swipe if enabled
    assert g.poll() == (False, 0)                          # no arrow
    g.on_up(1)
    assert g.poll() == (True, 0)                           # the drag still counts as a tap -> fire


def test_menu_swipe_emits_one_arrow_per_gesture():
    # a single drag toggles the mode ONCE; dragging further doesn't re-toggle (a second swipe needs a lift).
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.on_down(1, (600, 260))
    g.on_move(1, (600, 320), SIZE)                          # crosses threshold -> one arrow
    g.on_move(1, (600, 400), SIZE)                          # keeps dragging -> no repeat
    assert g.poll() == (False, touch.SCAN_DOWN)


def test_menu_second_finger_ignored_until_owner_lifts():
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.on_down(1, (600, 360))                                # finger 1 owns the gesture
    g.on_down(2, (300, 360))                                # a second finger is ignored
    g.on_up(2)                                              # its lift does nothing (not the owner)
    assert g.poll() == (False, 0)
    g.on_up(1)                                              # the owner's lift = the tap
    assert g.poll() == (True, 0)


def test_touchcontroller_tap_survives_many_ticks_until_consumed():
    # REGRESSION: present_front_scene calls pump() (hence TouchController.tick()) MANY times per presented frame
    # (the interpolation / smooth-fade sub-frames). The gesture must ACCUMULATE and be drained once per frame by
    # consume_menu -- NOT by tick -- or an internal pump eats the tap and the menu "barely responds to taps".
    import os
    import sys
    from pathlib import Path

    import pytest
    pygame = pytest.importorskip("pygame")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    pygame.init()
    from android_host import TouchController

    tc = TouchController(android=False)
    S = (1280, 720)

    def fev(kind, x, y):
        return pygame.event.Event(kind, finger_id=1, x=x / S[0], y=y / S[1], dx=0, dy=0, pressure=1.0)

    tc.set_screen("")                                       # a tap-only screen (oldies / menu)
    tc.handle_event(fev(pygame.FINGERDOWN, 640, 360), S)
    tc.handle_event(fev(pygame.FINGERUP, 640, 360), S)
    for _ in range(10):
        tc.tick(S, frontend=True)                           # 10 internal pumps must NOT drain the tap
    assert tc.consume_menu() == (True, 0)                   # the once-per-frame drain still sees it
    assert tc.consume_menu() == (False, 0)                  # and it's a single pulse


def test_scancodes_match_input_layer():
    from pre2.native import input as native_input
    assert touch.SCAN_LEFT == native_input.SCAN_LEFT
    assert touch.SCAN_RIGHT == native_input.SCAN_RIGHT
    assert touch.SCAN_UP == native_input.SCAN_UP
    assert touch.SCAN_DOWN == native_input.SCAN_DOWN
    assert touch.SCAN_FIRE == native_input.SCAN_FIRE

    from pre2.native.touch import Flags
    f = Flags(left=True, fire=True)
    assert f.scancodes() == {touch.SCAN_LEFT, touch.SCAN_FIRE}
