# nuked_opl3

Self-contained [cffi](https://cffi.readthedocs.io/) bindings for
**Nuked-OPL3**, Alexey Khokholov's cycle-accurate Yamaha OPL3/OPL2 emulator.

Nuked-OPL3 is the same core used by **DOSBox-X** and **VGMPlay**, so rendering
an OPL register trace through this binding reproduces what those players output
essentially sample-for-sample — unlike approximate cores, which differ in
feedback, envelope and DAC behaviour and make individual FM voices sound a bit
too loud or too quiet.

The package depends only on `cffi` at runtime and a C compiler at build time.
It deliberately does not import anything from the host application, so it can be
copied into its own repository and reused.

## Building

The compiled extension is **not** checked in; build it once:

```sh
python -m nuked_opl3._ffi_build
```

This requires a C compiler:

- **Windows:** Microsoft C++ Build Tools (the "Desktop development with C++"
  workload). distutils' automatic MSVC detection fails for very new Visual
  Studio releases, so build from the **"x64 Native Tools Command Prompt for
  VS"** (Start menu) — it puts `cl.exe` on PATH with the 64-bit toolchain set
  up. The build script detects that and sets `DISTUTILS_USE_SDK` for you, so
  just run `python -m nuked_opl3._ffi_build` there. Use the **x64** prompt
  (not the plain "Developer Command Prompt", which may be 32-bit) so the
  extension matches 64-bit Python.
- **Linux / macOS:** `gcc` or `clang` (usually already present, or via
  `build-essential` / Xcode command line tools).

After building, `nuked_opl3/_opl3_cffi*.{pyd,so}` exists and `import nuked_opl3`
works.

## Usage

```python
import numpy as np
from nuked_opl3 import OPL3

chip = OPL3(sample_rate=49716)      # OPL native rate; resamples if you pass another
chip.write(0x20, 0x01)             # AM/VIB/EGT/KSR/MULT for operator 0
chip.write(0x40, 0x10)             # KSL/TL
chip.write(0x60, 0xF0); chip.write(0x80, 0x77)
chip.write(0xA0, 0x98); chip.write(0xB0, 0x31)   # set F-number/block + key-on

pcm = np.frombuffer(chip.generate_mono(49716), dtype="<i2")   # 1 second mono
```

`write()` queues the register through the chip's timed write buffer (the
correct path for time-ordered playback interleaved with `generate_*`).
`write_immediate()` bypasses the buffer.

`generate_mono(n)` / `generate_stereo(n)` return raw little-endian `int16` PCM
bytes; wrap with `numpy.frombuffer(..., dtype="<i2")`. After `reset()` the chip
is in OPL2 (YM3812) compatible mode, where both output channels carry the same
mono mix.

## Extracting into its own repository

Move the contents of this directory so the import package sits at the repo root
as `nuked_opl3/` (i.e. `repo/nuked_opl3/__init__.py`, `repo/setup.py`,
`repo/pyproject.toml`, `repo/nuked_opl3/vendor/...`), then `pip install .`.

## Licensing

The vendored core (`vendor/opl3.c`, `vendor/opl3.h`) is **LGPL-2.1-or-later**,
© 2013-2020 Nuke.YKT. The binding code is released under the same terms. See
`LICENSE`.
