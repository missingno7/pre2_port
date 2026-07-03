"""Prehistorik 2 source-port target package.

Lazy exports (PEP 562): ``pre2.runtime`` builds the whole VM (dos_re CPU/DOS) at import time, and an EAGER
``from .runtime import ...`` here dragged the emulator into every ``import pre2.x`` — including the VM-less
native standalone (scripts/play_native.py + scripts/deploy_native.py), whose import closure must stay free of
the VM. The attributes resolve on first use, so VM users see no difference.
"""

__all__ = ["create_pre2_runtime", "load_pre2_snapshot", "build_command_tail"]


def __getattr__(name):
    if name in ("create_pre2_runtime", "load_pre2_snapshot"):
        from . import runtime
        return getattr(runtime, name)
    if name == "build_command_tail":
        from .launch import build_command_tail
        return build_command_tail
    raise AttributeError(f"module 'pre2' has no attribute {name!r}")
