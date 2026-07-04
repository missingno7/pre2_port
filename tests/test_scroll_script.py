"""The 1030:3922 scripted-camera-scroll state half + LEVELG snow render half.

Exercises the offset-free view API (``ScrollScriptView`` is the byte-backed layout bridge; the recovered logic
never sees an offset)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pre2.bridge.dgroup_view import DGROUP_BASE, ScrollScriptView
from pre2.native.state import NativeGameState
from pre2.recovered.scroll_script import scroll_script_snow, scroll_script_state

_BASE = DGROUP_BASE


def _view(words):
    d = bytearray(0x100000)
    for off, val in words.items():
        d[_BASE + off] = val & 0xFF
        d[_BASE + off + 1] = (val >> 8) & 0xFF
    # the LEVELG script table @0x2d76: {thr, delta, clamp} x3 then -1
    for i, (thr, dl, cl) in enumerate(((0x01B8, 1, 40), (0x0370, 6, 100), (0x0528, 1, 255))):
        b = 0x2D76 + i * 6
        for off, v in ((b, thr), (b + 2, dl), (b + 4, cl)):
            d[_BASE + off] = v & 0xFF
            d[_BASE + off + 1] = (v >> 8) & 0xFF
    d[_BASE + 0x2D88] = 0xFF
    d[_BASE + 0x2D89] = 0xFF
    return ScrollScriptView(NativeGameState(d))


def test_counter_only_off_gate():
    s = _view({0x2DBE: 0x100, 0x6BF6: 0, 0x2DBC: 0x2D76, 0x6BD5: 0x101})   # &3 != 0
    scroll_script_state(s)
    assert s.frame_counter == 0x101 and s.wind == 0          # only the counter ticks


def test_accumulate_and_clamp():
    s = _view({0x2DBE: 0x2AC, 0x6BF6: 0, 0x2DBC: 0x2D76, 0x6BD5: 0x2AC})    # past thr 0x1b8
    scroll_script_state(s)
    assert s.frame_counter == 0x2AD and s.wind == 1           # +delta(1)
    s = _view({0x2DBE: 0x2AC, 0x6BF6: 40, 0x2DBC: 0x2D76, 0x6BD5: 0x2AC})   # already at clamp 40
    scroll_script_state(s)
    assert s.wind == 40                                        # stays clamped


def test_advance_to_next_entry():
    # counter past the SECOND threshold (0x370) -> the entry pointer advances by 6 for the NEXT frame; THIS frame
    # still uses entry 0's delta(1)/clamp(40), since the entry was captured before the advance.
    s = _view({0x2DBE: 0x370, 0x6BF6: 0x40, 0x2DBC: 0x2D76, 0x6BD5: 0x374})
    scroll_script_state(s)
    assert s.script_ptr == 0x2D7C and s.wind == 40            # pointer advanced; scroll clamped to entry 0's 40
    s = _view({0x2DBE: 0x370, 0x6BF6: 0x40, 0x2DBC: 0x2D7C, 0x6BD5: 0x374})
    scroll_script_state(s)
    assert s.wind == 0x46                                      # next frame: entry 1 (delta 6, clamp 100) applies


def test_empty_script_is_counter_only():
    s = _view({0x2DBE: 5, 0x6BF6: 0, 0x2DBC: 0x2D88, 0x6BD5: 0})            # ptr -> the -1 marker
    scroll_script_state(s)
    assert s.frame_counter == 6 and s.wind == 0


# ---- the render half: the LEVELG falling snow (3922:396A..39DE) ---------------------------------------------

def test_snow_no_wind_is_noop():
    """Every non-LEVELG level has wind == 0 -> the snow half plots nothing and mutates nothing."""
    s = _view({0x6BF6: 0})
    s.rng_a, s.rng_b, s.rng_c, s.rng_d = 0x01, 0x02, 0x03, 0xF005
    assert scroll_script_snow(s) == []
    assert (s.rng_a, s.rng_b, s.rng_c, s.rng_d) == (0x01, 0x02, 0x03, 0xF005)   # rng untouched


def test_snow_byte_exact_vs_asm():
    """scroll_script_snow reproduces the ASM 3922 render half BYTE-EXACT: the shared gameplay rng, the flake
    array, and the plotted white pixels — captured from a live LEVELG snow frame (fixture)."""
    fix = json.loads((Path(__file__).parent / "fixtures" / "snow_levelg.json").read_text())
    d = bytearray(0x100000)
    d[_BASE + 0x2CEC:_BASE + 0x2CF2] = bytes.fromhex(fix["seed"])
    d[_BASE + 0x6CA9:_BASE + 0x6CA9 + 0x200] = bytes.fromhex(fix["flakes"])
    s = ScrollScriptView(NativeGameState(d))
    s.wind = fix["wind"]; s.camera_x = fix["cam"]; s.draw_page = fix["page"]
    plots = scroll_script_snow(s)
    assert bytes(d[_BASE + 0x2CEC:_BASE + 0x2CF2]).hex() == fix["exp_seed"]           # shared rng byte-exact
    assert hashlib.sha256(bytes(d[_BASE + 0x6CA9:_BASE + 0x6CA9 + 0x200])).hexdigest()[:16] == fix["exp_flakes_sha"]
    assert len(plots) == fix["exp_plot_count"]
    assert hashlib.sha256(str(plots).encode()).hexdigest()[:16] == fix["exp_plots_sha"]
