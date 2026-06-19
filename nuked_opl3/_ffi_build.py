"""cffi build script for the Nuked-OPL3 binding.

Run directly to compile the extension in place::

    python -m nuked_opl3._ffi_build

or let it be invoked automatically by setuptools via the ``cffi_modules``
hook in ``setup.py`` when the package is pip-installed.
"""
from __future__ import annotations

import glob
import importlib
import os
import shutil
import tempfile
import time

from cffi import FFI


def _prefer_active_msvc_env() -> None:
    """Use the current VS dev-prompt environment if a compiler is on PATH.

    setuptools/distutils tries to *locate* an MSVC install via ``vswhere`` and
    run ``vcvarsall.bat`` itself.  That detection fails for very new Visual
    Studio releases (e.g. VS 2026 / v18).  When the script is launched from a
    Visual Studio Developer Command Prompt, ``cl.exe`` is already on PATH and
    the toolchain env is fully configured -- setting ``DISTUTILS_USE_SDK``
    tells distutils to trust that environment instead of re-detecting it.
    """
    if os.name != "nt":
        return
    if shutil.which("cl") and not os.environ.get("DISTUTILS_USE_SDK"):
        os.environ["DISTUTILS_USE_SDK"] = "1"
        os.environ.setdefault("MSSdk", "1")

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")

ffibuilder = FFI()

# Only the handful of entry points we use.  ``typedef struct { ...; }`` lets
# cffi learn the real size/layout of opl3_chip from the included header at
# compile time (API mode), so OPL3() can allocate one with ffi.new().
ffibuilder.cdef(
    r"""
    typedef struct { ...; } opl3_chip;

    void OPL3_Reset(opl3_chip *chip, uint32_t samplerate);
    void OPL3_WriteReg(opl3_chip *chip, uint16_t reg, uint8_t v);
    void OPL3_WriteRegBuffered(opl3_chip *chip, uint16_t reg, uint8_t v);
    void OPL3_GenerateStream(opl3_chip *chip, int16_t *sndptr, uint32_t numsamples);
    """
)

ffibuilder.set_source(
    "nuked_opl3._opl3_cffi",
    '#include "opl3.h"',
    sources=[os.path.join(_VENDOR, "opl3.c")],
    include_dirs=[_VENDOR],
)


def build_in_place(verbose: bool = True) -> str:
    """Compile the extension and place it next to this package.

    cffi changes the working directory to ``tmpdir`` while it drives setuptools.
    Modern setuptools auto-discovers packages from the cwd, which fails if the
    cwd is a repository root containing several top-level packages.  To stay
    robust we compile inside an isolated temporary directory and then copy the
    resulting ``nuked_opl3/_opl3_cffi*`` artifact into this package directory.
    """
    _prefer_active_msvc_env()
    with tempfile.TemporaryDirectory() as tmp:
        ffibuilder.compile(tmpdir=tmp, verbose=verbose)
        produced = sorted(
            glob.glob(os.path.join(tmp, "nuked_opl3", "_opl3_cffi*"))
            + glob.glob(os.path.join(tmp, "_opl3_cffi*"))
        )
        artifacts = [p for p in produced if p.endswith((".pyd", ".so", ".dylib"))]
        if not artifacts:
            raise RuntimeError(f"cffi build produced no extension module in {tmp!r}")
        dest = ""
        for src in artifacts:
            dest = os.path.join(_HERE, os.path.basename(src))
            tmp_dest = dest + ".tmp"
            last_error: OSError | None = None
            for _attempt in range(10):
                try:
                    shutil.copy2(src, tmp_dest)
                    os.replace(tmp_dest, dest)
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    try:
                        if os.path.exists(tmp_dest):
                            os.remove(tmp_dest)
                    except OSError:
                        pass
                    time.sleep(0.5)
            if last_error is not None:
                if os.path.exists(dest):
                    try:
                        importlib.import_module("nuked_opl3._opl3_cffi")
                    except Exception:
                        pass
                    else:
                        if verbose:
                            print(f"kept existing locked extension: {dest}")
                        return dest
                raise last_error
        return dest


if __name__ == "__main__":
    out = build_in_place(verbose=True)
    print(f"built: {out}")
