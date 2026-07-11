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


def test_stick_has_no_up():
    # the stick is left/right/down only (jump is the JUMP button); dragging up registers nothing and the knob
    # is clamped to the base line (a lower half-circle).
    tc = TouchControls()
    base = (LAY.stick_rest[0], LAY.stick_rest[1])
    tc.update({1: base}, SIZE)
    flags, rm, _ = tc.update({1: _drag(base, 0, -LAY.stick_radius)}, SIZE)   # straight up
    assert not flags.up and not flags.down
    assert abs(rm.knob[1] - rm.stick_base[1]) < 1.0                          # knob does not rise above the base
    flags, _, _ = tc.update({1: _drag(base, LAY.stick_radius, -LAY.stick_radius)}, SIZE)   # up-right
    assert flags.right and not flags.up


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
