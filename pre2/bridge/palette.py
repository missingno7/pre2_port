"""VM↔memory layout for the palette fade (1030:6772).

Layout only — it reads the fade state + source/target palettes from DGROUP and writes
the DAC + fade flags back. The fade *math* lives in ``pre2.recovered.transition``.
"""
from __future__ import annotations

from dataclasses import dataclass

from dos_re.dos import _dac8

_DS = 0x1A0F                 # DGROUP segment (GOG build)
_SEL = 0x2D8A               # [asm 677F] index into the palette-pointer table
_PTR_TABLE = 0x2D00         # [asm 6787] word table: ptr = [_PTR_TABLE + sel*2]
_TARGET = 0xACB7            # [asm 6791] target palette (48 bytes, 6-bit)
_C01 = 0x6C01               # fade active flag
_C02 = 0x6C02               # fade direction flag (non-zero swaps src/target)
_C03 = 0x6C03               # fade amount (incremented each step)
_NCOMP = 0x30               # 48 DAC components (16 colours × RGB)


@dataclass(frozen=True)
class FadeInputs:
    """The state one fade step reads: the (already-incremented) amount, direction, and
    the source/target 6-bit palettes (48 bytes each)."""
    direction: int
    fade_amt: int
    src: bytes
    target: bytes


def _rb(mem, off: int) -> int:
    return mem.data[(_DS << 4) + off]


def _rw(mem, off: int) -> int:
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def _wb(mem, off: int, val: int) -> None:
    mem.data[(_DS << 4) + off] = val & 0xFF


def fade_active(mem) -> bool:
    """[asm 6772-6779] the fade runs only while ``[6C01] | [6C02] != 0``."""
    return (_rb(mem, _C01) | _rb(mem, _C02)) != 0


def read_fade_inputs(mem) -> FadeInputs:
    """Read one fade step's inputs (caller has checked :func:`fade_active`).

    Returns ``fade_amt`` already incremented ([asm 677B: inc [6C03]]) — i.e. the value
    the step uses and that must be written back."""
    direction = _rb(mem, _C02)
    fade_amt = (_rb(mem, _C03) + 1) & 0xFF
    ptr = _rw(mem, _PTR_TABLE + _rb(mem, _SEL) * 2)      # [asm 677F-6787]
    src = bytes(mem.data[(_DS << 4) + ptr:(_DS << 4) + ptr + _NCOMP])
    target = bytes(mem.data[(_DS << 4) + _TARGET:(_DS << 4) + _TARGET + _NCOMP])
    return FadeInputs(direction, fade_amt, src, target)


def write_dac(dos, out: bytes) -> None:
    """Write the 48 6-bit components to DAC colours 0..15 through the real VGA port path
    ([asm 678B: out 3C8,0; then 0x30× out 3C9]) so the result is byte-identical to the
    ASM, including the resulting DAC write-index/component state."""
    dos._track_vga_dac_ports(0x03C8, 0, 8)
    for v in out:
        dos._track_vga_dac_ports(0x03C9, v, 8)


def write_fade_state(mem, fade_amt: int, *, done: bool, direction: int, active: int) -> None:
    """Write back the incremented amount and, when the fade has finished, clear the
    active+direction flags ([asm 67C8-67D1: if bp==0 -> [6C01]=0; [6C02]=0])."""
    _wb(mem, _C03, fade_amt)
    if done:
        _wb(mem, _C01, 0)
        _wb(mem, _C02, 0)
    else:
        _wb(mem, _C01, active)
        _wb(mem, _C02, direction)


def predict_dac16(out: bytes) -> list[tuple[int, int, int]]:
    """The 16 8-bit RGB DAC colours the fade step produces (6-bit -> 8-bit the way the
    VGA DAC expands them) — for verify-mode prediction without touching the live DAC."""
    return [(_dac8(out[3 * i]), _dac8(out[3 * i + 1]), _dac8(out[3 * i + 2]))
            for i in range(16)]


def read_dac16(dos) -> list[tuple[int, int, int]]:
    """Snapshot DAC colours 0..15 (8-bit RGB) — for verify-mode diffing."""
    return [tuple(dos.vga_palette[i]) for i in range(16)]


def read_fade_flags(mem) -> tuple[int, int, int]:
    """(``[6C01]``, ``[6C02]``, ``[6C03]``) — for verify-mode diffing."""
    return _rb(mem, _C01), _rb(mem, _C02), _rb(mem, _C03)
