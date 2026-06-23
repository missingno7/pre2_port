"""The BONUS-letter flash-parity decision (1030:4683), recovered as the pure
:func:`pre2.recovered.hud.effective_bonus_mask`. This is the visual decision that used to live in
the bridge (`bridge/render_state._hud_state`); it now belongs with the HUD leaf so the recovered
module owns the full "which of B/O/N/U/S light up this frame" behavior. Asset-free pure logic."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.hud import effective_bonus_mask  # noqa: E402


def test_no_celebration_shows_collected_mask():
    # flash inactive -> the raw collected set is drawn as-is (parity ignored)
    for collected in (0x00, 0x05, 0x1F, 0x12):
        assert effective_bonus_mask(collected, False, 0) == collected
        assert effective_bonus_mask(collected, False, 1) == collected


def test_celebration_flashes_all_five_by_parity():
    # flash active -> all five letters on (0x1F) on odd parity, off (0) on even — collected ignored
    assert effective_bonus_mask(0x00, True, 1) == 0x1F
    assert effective_bonus_mask(0x1F, True, 1) == 0x1F
    assert effective_bonus_mask(0x0A, True, 1) == 0x1F   # collected value does not matter while flashing
    assert effective_bonus_mask(0x1F, True, 0) == 0x00
    assert effective_bonus_mask(0x00, True, 0) == 0x00


def test_parity_uses_low_bit_only():
    # frame_parity is the raw [0x6BD5] counter; only bit0 selects the flash phase
    assert effective_bonus_mask(0, True, 0x42) == 0x00   # even
    assert effective_bonus_mask(0, True, 0x43) == 0x1F   # odd


def test_collected_is_byte_masked():
    assert effective_bonus_mask(0x1FF, False, 0) == 0xFF
