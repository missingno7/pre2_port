"""Native host-input adapter — drive the game from host keys, no keyboard ISR.

DC1 (the input decoder, 1030:0DC1) reads per-scancode key flags at ``DS:[0x27F4 + scancode]`` — the table the
INT 09 keyboard ISR maintains (``[0x2841]`` = ``0x27F4 + 0x4D`` numpad-6/right, ``[0x282D]`` = ``0x27F4 + 0x39``
space, ...). A standalone runtime owns the keyboard itself: it writes that table directly from the host key
state each frame, and DC1 turns it into the six FSM input flags exactly as under the VM. The default movement
keys are the numpad arrows + space, the same scancodes DC1's primary sources read.
"""
from __future__ import annotations

from pre2.native.state import DATA_SEG

KEY_TABLE = 0x27F4          # DS:[0x27F4 + scancode] = "key down" flag (0xFF down / 0 up), read by DC1
_DS_BASE = (DATA_SEG << 4) & 0xFFFFF

# default action -> DOS scancode (DC1's primary source for each of the six input flags)
SCAN_FIRE = 0x39           # space   -> [0x27E8]
SCAN_UP = 0x48             # numpad-8 -> [0x27EA]
SCAN_DOWN = 0x50           # numpad-2 -> [0x27EB]
SCAN_RIGHT = 0x4D          # numpad-6 -> [0x27EC]
SCAN_LEFT = 0x4B           # numpad-4 -> [0x27ED]


def set_key(state, scancode: int, down: bool) -> None:
    """Set the raw key-table flag for one scancode (what the INT 09 ISR would do on a make/break code)."""
    state.data[(_DS_BASE + KEY_TABLE + (scancode & 0x7F)) & 0xFFFFF] = 0xFF if down else 0


def apply_input(state, *, left=False, right=False, up=False, down=False, fire=False) -> None:
    """Write the raw key table for the default movement keys from a host key set (call once per frame, before
    the frame step). DC1 reads the table and produces the player's input flags."""
    set_key(state, SCAN_LEFT, left)
    set_key(state, SCAN_RIGHT, right)
    set_key(state, SCAN_UP, up)
    set_key(state, SCAN_DOWN, down)
    set_key(state, SCAN_FIRE, fire)
