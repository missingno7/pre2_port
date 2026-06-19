from __future__ import annotations

from pathlib import Path

import pytest

import nuked_opl3


ROOT = Path(__file__).resolve().parents[1]


def test_vendored_nuked_opl3_package_is_present_without_build_artifacts():
    pkg = ROOT / "nuked_opl3"
    assert (pkg / "__init__.py").is_file()
    assert (pkg / "_ffi_build.py").is_file()
    assert (pkg / "vendor" / "opl3.c").is_file()
    assert (pkg / "vendor" / "opl3.h").is_file()
    assert (pkg / "LICENSE").is_file()
    assert not list(pkg.glob("_opl3_cffi*.pyd"))
    assert not list(pkg.glob("_opl3_cffi*.so"))
    assert not list(pkg.glob("_opl3_cffi*.dylib"))


def test_vendored_nuked_opl3_import_is_lazy_until_extension_is_built():
    assert hasattr(nuked_opl3, "OPL3")
    assert isinstance(nuked_opl3.is_available(), bool)
    if not nuked_opl3.is_available():
        with pytest.raises(nuked_opl3.NukedOpl3Unavailable):
            nuked_opl3.OPL3()
