"""Pure-cffi Python bindings for the Nuked-OPL3 Yamaha OPL3/OPL2 emulator.

Nuked-OPL3 (by Alexey "Nuke.YKT" Khokholov) is the cycle-accurate OPL core
used by DOSBox-X and VGMPlay, so rendering an OPL register trace through it
matches those players essentially sample-for-sample.

This package is intentionally self-contained and dependency-light (only
``cffi`` at runtime, plus a C compiler at build time).  It is designed to be
lifted out into its own repository and reused: nothing here imports from the
hosting application.

Build the compiled extension once with::

    python -m nuked_opl3._ffi_build

(That requires a C compiler -- MSVC Build Tools on Windows, gcc/clang on
Unix.)  After that, ``import nuked_opl3`` works.

Typical use (OPL2 / YM3812 compatible playback -- the default after reset)::

    import numpy as np
    from nuked_opl3 import OPL3

    chip = OPL3(sample_rate=49716)
    chip.write(0x20, 0x01)      # buffered, chip-accurate register write
    chip.write(0xA0, 0x98)
    chip.write(0xB0, 0x31)      # key-on
    pcm = np.frombuffer(chip.generate_mono(49716), dtype="<i2")  # 1 s mono
"""
from __future__ import annotations

__all__ = ["OPL3", "NukedOpl3Unavailable", "OPL_NATIVE_RATE", "__version__"]
__version__ = "0.1.0"

#: Native output rate of the OPL3 (master clock 14.318 MHz / 288).
OPL_NATIVE_RATE = 49716


class NukedOpl3Unavailable(ImportError):
    """Raised when the compiled Nuked-OPL3 cffi extension is not available."""


try:  # pragma: no cover - exercised only when the extension is built
    from ._opl3_cffi import ffi as _ffi, lib as _lib

    _AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001 - re-raised lazily with guidance
    _ffi = None  # type: ignore[assignment]
    _lib = None  # type: ignore[assignment]
    _AVAILABLE = False
    _IMPORT_ERROR = _exc


def is_available() -> bool:
    """Return True when the compiled extension can be used."""
    return _AVAILABLE


def _require_extension() -> None:
    if not _AVAILABLE:
        raise NukedOpl3Unavailable(
            "The Nuked-OPL3 C extension is not built. Build it once with "
            "`python -m nuked_opl3._ffi_build` (needs a C compiler: MSVC "
            "Build Tools on Windows, gcc/clang elsewhere)."
        ) from _IMPORT_ERROR


class OPL3:
    """A single Nuked-OPL3 chip instance.

    After :meth:`reset` the chip is in OPL2 (YM3812) compatible mode, which is
    what Ancient Empires (and most AdLib-era titles) drive.  Writing the OPL3
    "new" bit (register ``0x105``) switches it into full OPL3 mode.

    ``generate_*`` methods return raw little-endian ``int16`` PCM bytes so that
    this package stays free of a hard NumPy dependency; wrap the result with
    ``numpy.frombuffer(..., dtype="<i2")`` on the caller side if desired.
    """

    def __init__(self, sample_rate: int = OPL_NATIVE_RATE) -> None:
        _require_extension()
        self.sample_rate = int(sample_rate)
        self._chip = _ffi.new("opl3_chip *")
        _lib.OPL3_Reset(self._chip, self.sample_rate)

    def reset(self, sample_rate: int | None = None) -> None:
        """Reset the chip, optionally re-targeting a new output sample rate."""
        if sample_rate is not None:
            self.sample_rate = int(sample_rate)
        _lib.OPL3_Reset(self._chip, self.sample_rate)

    def write(self, reg: int, value: int) -> None:
        """Queue a register write through the chip's timed write buffer.

        This models the real chip's short write latency and is the correct
        entry point for time-ordered playback interleaved with ``generate``.
        """
        _lib.OPL3_WriteRegBuffered(self._chip, int(reg) & 0x1FF, int(value) & 0xFF)

    def write_immediate(self, reg: int, value: int) -> None:
        """Apply a register write immediately (no write-buffer latency)."""
        _lib.OPL3_WriteReg(self._chip, int(reg) & 0x1FF, int(value) & 0xFF)

    def generate_stereo(self, num_frames: int) -> bytes:
        """Render ``num_frames`` interleaved stereo (L,R) int16 frames."""
        num_frames = max(0, int(num_frames))
        if num_frames == 0:
            return b""
        buf = _ffi.new("int16_t[]", num_frames * 2)
        _lib.OPL3_GenerateStream(self._chip, buf, num_frames)
        return bytes(_ffi.buffer(buf))

    def generate_mono(self, num_frames: int) -> bytes:
        """Render ``num_frames`` mono int16 frames (left channel).

        In OPL2-compatible mode both output channels carry the same mono mix,
        so the left channel is the full signal.
        """
        stereo = self.generate_stereo(num_frames)
        if not stereo:
            return stereo
        # Take every other int16 (the left channel) without requiring NumPy.
        # memoryview.tobytes() copies a strided/non-contiguous view into a
        # fresh contiguous bytes object, so this is safe.
        return memoryview(stereo).cast("h")[0::2].tobytes()
