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


def test_menu_tap_is_fire_on_release():
    # front-end gestures: a TAP (finger down then up, no swipe) is one fire pulse, emitted on the LIFT.
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    fire, arrow = g.update({1: (600, 360)}, SIZE)            # finger down
    assert not fire and arrow == 0                           # holding isn't a tap yet
    fire, arrow = g.update({1: (605, 362)}, SIZE)            # tiny drift, still held
    assert not fire and arrow == 0
    fire, arrow = g.update({}, SIZE)                         # lift -> the tap completes
    assert fire and arrow == 0


def test_menu_swipe_down_is_down_arrow_and_never_fires():
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.update({1: (600, 280)}, SIZE)                          # finger down
    fire, arrow = g.update({1: (600, 280 + 60)}, SIZE)       # drag down 60px (> 0.06*720 = 43px threshold)
    assert arrow == touch.SCAN_DOWN and not fire
    fire, arrow = g.update({}, SIZE)                         # lift -> a swipe must NOT also fire
    assert not fire and arrow == 0


def test_menu_swipe_up_is_up_arrow():
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.update({1: (600, 400)}, SIZE)
    fire, arrow = g.update({1: (600, 400 - 60)}, SIZE)       # drag up past the threshold
    assert arrow == touch.SCAN_UP and not fire


def test_menu_swipe_emits_one_arrow_per_gesture():
    # a single drag toggles the mode ONCE; dragging further doesn't re-toggle (a second swipe needs a lift).
    from pre2.native.touch import MenuGestures
    g = MenuGestures()
    g.update({1: (600, 260)}, SIZE)
    _, a1 = g.update({1: (600, 320)}, SIZE)                  # crosses threshold -> one arrow
    _, a2 = g.update({1: (600, 400)}, SIZE)                  # keeps dragging -> no repeat
    assert a1 == touch.SCAN_DOWN and a2 == 0


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
