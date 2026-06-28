"""Recovered transforms of the ProTracker song loader's three hot MOD-setup loops (``1030:2F4``).

These are the pure, byte-exact bodies of the loops the loader spends ~263k instructions in when a big module
(the final-boss music) is loaded — the "spot the boss" freeze. They are reproduced here so the live runtime
can fast-forward the loops in Python (``pre2.bridge.song_load_fastforward``) instead of grinding the ASM.

Proven byte-exact vs the ASM: each loop's result was diffed in place across a real boss-music load, and the
whole ``0x2F4`` routine was re-run with the loops short-circuited and matched the pure-ASM run byte-for-byte
(only the SQZ DOS-handle scratch, a harness artifact, differed).
"""
from __future__ import annotations

from pre2.islands import oracle_link


@oracle_link("1030:03F8", "MOD sample byte -> mixer scaled-unsigned: ((b+0x80)&0xFF)>>2", "VERIFIED",
             merge_target="audio_system")
def scale_samples(data: bytes) -> bytes:
    """[asm 03F8] Convert each MOD 8-bit signed sample byte to the mixer's scaled-unsigned format:
    ``add al,0x80 ; shr al,1 ; shr al,1`` == ``((b + 0x80) & 0xFF) >> 2`` (the +0x80 wraps at 8 bits first)."""
    return bytes(((b + 0x80) & 0xFF) >> 2 for b in data)


@oracle_link("1030:0425", "MOD note period -> 1-based period-table index (0x24 = not found)", "VERIFIED",
             merge_target="audio_system")
def remap_pattern_periods(cells: bytes, period_table: list[int]) -> bytes:
    """[asm 0425] For each 4-byte note cell, replace the 12-bit period word with its index in ``period_table``.

    The ASM reads the cell word ``b0|b1<<8``; if non-zero it builds the 12-bit period ``((b0&0x0F)<<8)|b1``
    and ``repne scasw`` searches the 0x24-entry table — storing the **1-based** match position (the count of
    compares done), or ``0x24`` when not found, back into the period word (little-endian). The effect word
    (bytes 2-3) is untouched, and a zero period word is left as-is."""
    out = bytearray(cells)
    for c in range(0, len(out) - 3, 4):
        if out[c] | (out[c + 1] << 8):
            period = ((out[c] & 0x0F) << 8) | out[c + 1]
            idx = period_table.index(period) + 1 if period in period_table else 0x24
            out[c] = idx & 0xFF
            out[c + 1] = (idx >> 8) & 0xFF
    return bytes(out)
