"""Bridge for the input-decode island (1030:0DC1 decode_input).

Presents the live VM DGROUP (0x1A0F) as the recovered decoder's ``(rb, rw)`` accessors and applies its
``{offset: (value, width)}`` write contract in place. Pure layout/translation — reuses the shared DGROUP
byte/word readers + write-apply from the effects-update island.
"""
from __future__ import annotations

from pre2.views.memory_adapter import apply_ds, readers

__all__ = ["readers", "apply_ds"]
