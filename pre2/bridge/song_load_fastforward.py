"""Live fast-forward of the ProTracker song loader's three heavy MOD-setup loops (``1030:03F8/06B3/0425``).

When a big module loads (the final-boss music), ``0x2F4`` spends ~263k instructions in three pure loops —
sample scaling, a sample-bank copy, and pattern note→index remapping — which shows as a ~1s freeze the moment
the boss appears. Each hook here computes the loop's full result in Python (byte-exact, see
:mod:`pre2.recovered.song_load`) and jumps the CPU past the ASM loop, collapsing the freeze to ~instant.

Proven byte-exact: the whole ``0x2F4`` re-run with these three loops short-circuited matched the pure-ASM run
byte-for-byte (scratchpad recon_songload). This is a LIVE optimisation only — it changes how many (2142-instr)
frames the deterministic song load spans, so it must be applied consistently for record + replay and is kept
OUT of the verify/oracle path. Install it on the live viewer; leave it off for deterministic replay/verify
unless the demo was recorded with it.
"""
from __future__ import annotations

from pre2.recovered.song_load import remap_pattern_periods, scale_samples

_SCALE = (0x1030, 0x03F8)   # sample scale loop  -> falls through at 0x0405
_COPY = (0x1030, 0x06B3)    # sample-bank copy   -> falls through at 0x06C5
_REMAP = (0x1030, 0x0425)   # pattern remap loop -> falls through at 0x0444


def _ff_scale(c) -> None:
    """[asm 03F8..0403] es:[di..di+bp) = scale_samples(...) in place; di += bp, bp = 0, al = last byte."""
    s = c.s
    es, di, bp = s.es & 0xFFFF, s.di & 0xFFFF, s.bp & 0xFFFF
    base = (es << 4) & 0xFFFFF
    region = bytes(c.mem.data[(base + ((di + k) & 0xFFFF)) & 0xFFFFF] for k in range(bp))
    out = scale_samples(region)
    for k in range(bp):
        c.mem.data[(base + ((di + k) & 0xFFFF)) & 0xFFFFF] = out[k]
    s.di = (di + bp) & 0xFFFF
    s.bp = 0
    s.ax = (s.ax & 0xFF00) | (out[-1] if out else (s.ax & 0xFF))
    s.ip = 0x0405


def _ff_copy(c) -> None:
    """[asm 06B3..06C3] copy bx*16 bytes from paragraph ax to paragraph dx (the loop walks both segments)."""
    s = c.s
    ax, dx, bx = s.ax & 0xFFFF, s.dx & 0xFFFF, s.bx & 0xFFFF
    n = bx * 16
    src = (ax << 4) & 0xFFFFF
    dst = (dx << 4) & 0xFFFFF
    c.mem.data[dst:dst + n] = c.mem.data[src:src + n]
    s.ax = (ax + bx) & 0xFFFF
    s.dx = (dx + bx) & 0xFFFF
    s.bx = 0
    s.cx = 0
    s.si = 0x10
    s.di = 0x10
    s.ip = 0x06C5


def _ff_remap(c) -> None:
    """[asm 0425..0442] remap dx note cells in place via the period table at es:[0xE75]; si += dx*4, dx = 0."""
    s = c.s
    ds, si, dx, es = s.ds & 0xFFFF, s.si & 0xFFFF, s.dx & 0xFFFF, s.es & 0xFFFF
    tb = (es << 4) & 0xFFFFF
    table = [c.mem.data[(tb + 0xE75 + i * 2) & 0xFFFFF] | (c.mem.data[(tb + 0xE76 + i * 2) & 0xFFFFF] << 8)
             for i in range(0x24)]
    db = (ds << 4) & 0xFFFFF
    n = dx * 4
    region = bytes(c.mem.data[(db + ((si + k) & 0xFFFF)) & 0xFFFFF] for k in range(n))
    out = remap_pattern_periods(region, table)
    for k in range(n):
        c.mem.data[(db + ((si + k) & 0xFFFF)) & 0xFFFFF] = out[k]
    s.si = (si + n) & 0xFFFF
    s.dx = 0
    s.cx = 0
    s.ip = 0x0444


def install_song_load_fastforward(cpu) -> None:
    """Install the three song-loader loop fast-forwards on ``cpu`` (live viewer only)."""
    cpu.replacement_hooks[_SCALE] = _ff_scale
    cpu.replacement_hooks[_COPY] = _ff_copy
    cpu.replacement_hooks[_REMAP] = _ff_remap
    cpu.hook_names[_SCALE] = "song_scale_ff"
    cpu.hook_names[_COPY] = "song_copy_ff"
    cpu.hook_names[_REMAP] = "song_remap_ff"
