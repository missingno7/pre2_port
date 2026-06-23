"""The renderer's persistent palette state machine (pre2.bridge.palette.read_palette_state).

A palette fade is renderer-owned semantic state that evolves each frame (e.g. an item-pickup
fade while gameplay runs), not a VGA side effect. `read_palette_state` exposes it as a
`PaletteState`: the displayed colours + the fade phase/progress/endpoints + the active named
palette. Deterministic (synthetic memory); the fade *math* is verified in test_transition and
the live read is exercised by gameplay snapshots.
"""
from __future__ import annotations

from types import SimpleNamespace

from pre2.bridge.palette import _DS, read_palette_state
from pre2.recovered.render_model import FadePhase, PaletteState

_BASE = _DS << 4


def _mem(setup):
    data = bytearray(0x30000)
    for off, val in setup.items():
        if isinstance(val, (bytes, bytearray)):
            data[_BASE + off:_BASE + off + len(val)] = val
        else:
            data[_BASE + off] = val & 0xFF
    return SimpleNamespace(data=data)


def _dos():
    return SimpleNamespace(vga_palette=[(i * 4, i * 4 + 1, i * 4 + 2) for i in range(16)])


def test_palette_state_idle():
    ps = read_palette_state(_mem({0x6C01: 0, 0x6C02: 0, 0x6C03: 0, 0x2D8A: 5}), _dos())
    assert isinstance(ps, PaletteState)
    assert ps.phase == FadePhase.NONE and ps.base_index == 5
    assert len(ps.colors) == 16 and ps.colors[0] == (0, 1, 2)
    assert ps.fade_from == b"" and ps.fade_amount == 0


def test_palette_state_fading_in():
    src, tgt = bytes(range(48)), bytes(range(100, 148))
    mem = _mem({0x6C01: 1, 0x6C02: 0, 0x6C03: 10, 0x2D8A: 3,
                0x2D06: 0x00, 0x2D07: 0x30, 0x3000: src, 0xACB7: tgt})  # [0x2D00+3*2] -> 0x3000
    ps = read_palette_state(mem, _dos())
    assert ps.phase == FadePhase.IN and ps.base_index == 3
    assert ps.fade_amount == 11                      # [0x6C03] + 1 (the step value)
    assert ps.fade_from == src and ps.fade_to == tgt  # IN: step src toward target


def test_palette_state_fading_out_swaps_endpoints():
    src, tgt = bytes(range(48)), bytes(range(100, 148))
    mem = _mem({0x6C01: 0, 0x6C02: 1, 0x6C03: 5, 0x2D8A: 3,
                0x2D06: 0x00, 0x2D07: 0x30, 0x3000: src, 0xACB7: tgt})
    ps = read_palette_state(mem, _dos())
    assert ps.phase == FadePhase.OUT                 # active via direction flag alone
    assert ps.fade_from == tgt and ps.fade_to == src  # OUT: direction swaps the endpoints
