"""Verification test for the recovered world-map CRTC pan (``pre2.recovered.world_map.map_pan``).

The formula (1030:97A8-97F7) was proven against the VM by replaying the menu-nav demo through the 96D5 world
map and checking the predicted ``(display_start, pel)`` against the VM's CRTC pan state at every retrace —
**303/303 frames** (with a one-frame sampling lag, since the pan registers reflect the previous frame's camera
when read at the retrace wait). The cases below pin the formula, including the wrapping-word camera (the map
pans left of the origin, e.g. ``0xFFFC``) and the display-start wrap.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from pre2.recovered.world_map import (
    LS_AUTO_ADVANCE,
    LS_MODE_SELECT,
    LS_PASSWORD,
    LS_WAIT,
    MapCamera,
    level_select_dispatch,
    map_camera_update,
    map_page_flip,
    map_pan,
    mode_select_input,
    mode_select_text_runs,
    password_accumulate,
    password_hex_value,
    password_text_runs,
    should_scroll_shift,
)

ROOT = Path(__file__).resolve().parents[1]
SINE_FIXTURE = ROOT / "tests" / "fixtures" / "world_map_bounce_sine.bin"
GOLD_CAMERA = "09d99eacf1b903249c7e7262da81e22f915e4f1c"   # 200 frames from origin, bounce on (verified vs VM)


def test_map_pan_origin():
    assert map_pan(0, 0) == (0, 0)


def test_map_pan_wrapping_camera():
    # the camera is a wrapping word; panning left of the origin gives a large display-start (>> 3) + pel
    assert map_pan(0xFFFC, 0) == (0x1FFF, 4)        # 0xFFFC >> 3 = 0x1FFF ; 0xFFFC & 7 = 4 (verified vs VM)
    assert map_pan(0xFFF8, 0) == (0x1FFF, 0)
    assert map_pan(0xFFF0, 0) == (0x1FFE, 0)


def test_map_pan_row_stride():
    assert map_pan(0, 1) == (0x28, 0)               # one row = 40 bytes
    assert map_pan(0, 2) == (0x50, 0)


def test_map_pan_display_start_wraps_at_0x2000():
    # 0x28 * 0x100 = 0x2800 -> masked to the 0x2000-word window -> 0x800
    assert map_pan(0, 0x100) == (0x800, 0)


def test_map_pan_pel_is_low_three_bits():
    for x in range(8):
        assert map_pan(x, 0)[1] == x
    assert map_pan(0x1F, 0)[1] == 7                 # only the low 3 bits


# --- 9ae0 bouncing camera update -----------------------------------------------------------------

def test_camera_scrolls_left_and_derives_fields():
    sine = SINE_FIXTURE.read_bytes()
    cam = MapCamera(x=0x100, row=5, phase=0, prev_x=0, prev_row=0, blit_off=0)
    nxt = map_camera_update(cam, bounce=True, sine_table=sine)
    assert nxt.x == 0x100 - 4                       # scrolls left 4/frame
    assert nxt.prev_x == 0x100                      # old X stashed
    assert nxt.phase == 1                           # phase pre-incremented
    assert nxt.blit_off == (0x100 & 7) * 0x1080     # = 0 here
    cam2 = MapCamera(x=0x107, row=0, phase=0, prev_x=0, prev_row=0, blit_off=0)
    assert map_camera_update(cam2, bounce=True, sine_table=sine).blit_off == 7 * 0x1080


def test_camera_bounce_off_leaves_row_unchanged():
    sine = SINE_FIXTURE.read_bytes()
    cam = MapCamera(x=0x40, row=0x20, phase=10, prev_x=0, prev_row=0x99, blit_off=0)
    nxt = map_camera_update(cam, bounce=False, sine_table=sine)
    assert nxt.row == 0x20                          # row untouched when bounce is off
    assert nxt.prev_row == 0x99                     # prev_row left as-is (ASM skips that block)
    assert nxt.x == 0x40 - 4 and nxt.phase == 11    # scroll + phase still advance


# --- 991F mode-select screen (draw + input) ------------------------------------------------------

def test_mode_select_text_runs_match_vm_witness():
    # the VM's draw_string args on demo_menunav (map_witness.py): "MODE" pen 0xC38 adv 4, then the difficulty
    # "BEGINNER" / " EXPERT " at pen 0x1185 adv 3.
    mode, diff = mode_select_text_runs(0)
    assert (mode.addr, mode.pen, mode.advance) == (0xB180, 0x0C38, 4)      # "MODE"
    assert (diff.addr, diff.pen, diff.advance) == (0xB185, 0x1185, 3)      # "BEGINNER"
    assert mode_select_text_runs(1)[1].addr == 0xB18E                      # " EXPERT " when [0xB197]!=0


def test_mode_select_input_arrow_toggles():
    for sc in (0x48, 0x4B, 0x4D, 0x50):          # up / left / right / down
        r = mode_select_input(sc, fire=False)
        assert r.toggle and r.consume and not r.confirm


def test_mode_select_input_fire_confirms_without_consuming():
    r = mode_select_input(0x48, fire=True)       # fire takes priority over the arrow
    assert r.confirm and not r.toggle and not r.consume


def test_mode_select_input_release_is_noop():
    r = mode_select_input(0x48 | 0x80, fire=False)   # key-release event (bit7)
    assert not (r.toggle or r.confirm or r.consume)


def test_mode_select_input_non_arrow_consumes_only():
    r = mode_select_input(0x02, fire=False)      # '1' key — consumed but no toggle
    assert r.consume and not r.toggle and not r.confirm


# --- 96D5 controller plumbing (page flip + scroll-shift gate) -------------------------------------

def test_map_page_flip():
    # the draw page becomes the new display-start; the clear page becomes the previous draw page
    assert map_page_flip(0x14b3, 0x0d30) == (0x14b3, 0x0d30)
    assert map_page_flip(0, 0x1750) == (0, 0x1750)


def test_should_scroll_shift_on_8_boundary():
    assert not should_scroll_shift(0x100, 0x100)        # same fine-X
    assert not should_scroll_shift(0x100, 0x101)        # within the same 8-block (bit3 unchanged)
    assert should_scroll_shift(0x107, 0x108)            # crossing the 8-boundary toggles bit3
    assert should_scroll_shift(0x108, 0x100)            # and back
    # the camera pans left 4/frame: a shift fires every other frame as bit3 toggles
    x, prev = 0x100, 0x104
    fires = sum(should_scroll_shift(prev, (prev := (prev - 4) & 0xFFFF)) for _ in range(8))
    assert fires == 4


# --- 9985 password-entry screen ------------------------------------------------------------------

def test_password_text_runs_match_vm_witness():
    label, buf = password_text_runs()
    assert (label.addr, label.pen, label.advance) == (0xB175, 0x0AF2, 3)   # "ENTER CODE"
    assert (buf.addr, buf.pen, buf.advance) == (0xB170, 0x12C9, 4)         # the typed code buffer


def test_password_hex_value():
    assert [password_hex_value(ord(c)) for c in "0123456789"] == list(range(10))
    assert [password_hex_value(ord(c)) for c in "ABCDEF"] == list(range(10, 16))
    assert password_hex_value(0x2D) is None                                # '-' sentinel = non-hex key


def test_password_accumulate_builds_code():
    code = 0
    for c in "ABCD":                                                       # the demo typed this
        code = password_accumulate(code, password_hex_value(ord(c)))
    assert code == 0xABCD
    # the accumulator is a 16-bit shift register (only the last 4 nibbles survive)
    code = 0
    for c in "12345":
        code = password_accumulate(code, password_hex_value(ord(c)))
    assert code == 0x2345


# --- 8E45 level-select dispatcher ----------------------------------------------------------------

def test_level_select_dispatch_priorities():
    # the mode flag wins over everything; then the password flag; then a keypress
    assert level_select_dispatch(1, 1, 1, 0) == LS_MODE_SELECT
    assert level_select_dispatch(0, 1, 1, 0) == LS_PASSWORD
    assert level_select_dispatch(0, 0, 1, 0) == LS_MODE_SELECT     # input -> mode-select
    assert level_select_dispatch(0, 0, 0, 0) == LS_WAIT            # idle -> keep counting


def test_level_select_dispatch_timeout_auto_advances():
    assert level_select_dispatch(0, 0, 0, 0x10D) == LS_WAIT        # one short of the timeout
    assert level_select_dispatch(0, 0, 0, 0x10E) == LS_AUTO_ADVANCE
    assert level_select_dispatch(0, 0, 0, 0x200) == LS_AUTO_ADVANCE


@pytest.mark.skipif(not SINE_FIXTURE.exists(), reason="sine-table fixture not present")
def test_camera_sequence_golden_vs_vm():
    sine = SINE_FIXTURE.read_bytes()
    cam = MapCamera(0, 0, 0, 0, 0, 0)
    seq = []
    for _ in range(200):
        cam = map_camera_update(cam, bounce=True, sine_table=sine)
        seq.append((cam.x, cam.row, cam.phase, cam.prev_x, cam.prev_row, cam.blit_off))
    blob = b"".join(struct.pack("<6H", *s) for s in seq)
    assert hashlib.sha1(blob).hexdigest() == GOLD_CAMERA
