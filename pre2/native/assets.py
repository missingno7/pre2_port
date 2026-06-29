"""Native asset loader — the standalone replacement for the .SQZ loader at 1030:107B.

Booting from game files means loading them the way the original does. 107B is the one asset entry point: given a
filename (a C-string in DGROUP, passed in DX), it reads the file from the game directory, SQZ-decompresses it,
writes the result to the stacking load pointer ``[0x2875]``, bumps that pointer by the asset's paragraph count,
and returns the segment the asset landed at. Every asset — levels, backgrounds, sprite sheets, fonts, the
palette — loads through here, stacking consecutively in memory.

``load_sqz`` reproduces that exactly over a NativeGameState + a game directory (no VM, no DOS). It reuses the
recovered codec (``unpack_sqz`` / ``sqz_bump_advance``); the only new part is reading the file from disk and the
load-pointer bookkeeping the ASM's allocator does.
"""
from __future__ import annotations

from pathlib import Path

from pre2.codecs.sqz import sqz_bump_advance, unpack_sqz
from pre2.native.state import DATA_SEG

LOAD_PTR = 0x2875          # DS:[0x2875] — the stacking load-buffer pointer (segment of the next free paragraph)
_DS_BASE = (DATA_SEG << 4) & 0xFFFFF


def _ww(state, off: int, val: int) -> None:
    b = (_DS_BASE + (off & 0xFFFF)) & 0xFFFFF
    state.data[b] = val & 0xFF
    state.data[(b + 1) & 0xFFFFF] = (val >> 8) & 0xFF


def read_cstring(state, off: int) -> str:
    """Read the NUL-terminated ASCII filename at DS:off (how the loader receives it in DX)."""
    base = (_DS_BASE + (off & 0xFFFF)) & 0xFFFFF
    end = state.data.index(0, base)
    return bytes(state.data[base:end]).decode("ascii", "replace")


def resolve_game_path(game_root: str, name: str) -> Path:
    """Case-insensitive resolve of an asset name in the game directory (the assets are uppercase; the loader's
    names are uppercase too, but be tolerant)."""
    root = Path(game_root)
    p = root / name
    if p.exists():
        return p
    upper = name.upper()
    for f in root.iterdir():
        if f.name.upper() == upper:
            return f
    raise FileNotFoundError(name)


def load_sqz(state, name: str, *, game_root: str) -> int:
    """Load + decompress one .SQZ asset into the stacking buffer at [0x2875], bump the pointer, and return the
    segment it landed at (what 107B returns in AX). [asm 1030:107B + the allocator bump @147D/1208/10E6]"""
    raw = resolve_game_path(game_root, name).read_bytes()
    out = unpack_sqz(raw)
    out_seg = state.rw(LOAD_PTR)
    base = (out_seg << 4) & 0xFFFFF
    state.data[base:base + len(out)] = out
    _ww(state, LOAD_PTR, (out_seg + sqz_bump_advance(raw)) & 0xFFFF)
    return out_seg


def load_sqz_by_dx(state, dx: int, *, game_root: str) -> int:
    """As called from ASM: the filename is the C-string at DS:dx."""
    return load_sqz(state, read_cstring(state, dx), game_root=game_root)
