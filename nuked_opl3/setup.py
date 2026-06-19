"""Setuptools shim so the package can be pip-installed as a standalone repo.

The actual extension is produced by cffi via the ``cffi_modules`` hook, which
points at the ``ffibuilder`` object in ``_ffi_build.py``.  Metadata lives in
``pyproject.toml``.

Within the Ancient Empires editor repository you normally do *not* need this
file -- just build in place with ``python -m nuked_opl3._ffi_build``.  It is
included so the directory can be extracted into its own repository (with this
package placed at the repo root as ``nuked_opl3/``) and installed with
``pip install .``.
"""
from setuptools import setup


if __name__ == "__main__":
    setup(
        cffi_modules=["nuked_opl3/_ffi_build.py:ffibuilder"],
    )
