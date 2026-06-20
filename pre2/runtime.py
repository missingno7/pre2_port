"""Prehistorik 2 runtime construction on top of the reusable DOS VM."""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import install_lzexe_0069_accelerator
from dos_re.runtime import Runtime, create_runtime as create_dos_runtime
from dos_re.snapshot import load_snapshot as load_dos_snapshot
from pre2.bootstrap_hooks import install_fast_adlib_service
from pre2.replacements import install_pre2_replacements, uninstall_pre2_replacements

ORIGINAL_EXE = "pre2.exe"


def resolve_pre2_exe_path(exe_path: str | Path) -> Path:
    return Path(exe_path)


def _install_bootstrap_helpers(rt: Runtime) -> None:
    # PRE2.EXE is LZEXE 0.91 packed.  Keep the unpacker out of the game-specific
    # source layer, but accelerate it so cold starts reach real program code.
    install_lzexe_0069_accelerator(rt.cpu, name_prefix="pre2")


def create_pre2_runtime(
    exe_path: str | Path,
    *,
    game_root: str | Path | None = None,
    command_tail: bytes | str = b"",
    fast_adlib: bool = False,
    native_replacements: bool = True,
) -> Runtime:
    rt = create_dos_runtime(
        resolve_pre2_exe_path(exe_path),
        game_root=game_root,
        command_tail=command_tail,
    )
    _install_bootstrap_helpers(rt)
    if fast_adlib:
        install_fast_adlib_service(rt)
    if native_replacements:
        install_pre2_replacements(rt)
    else:
        uninstall_pre2_replacements(rt)  # dos_re auto-installs the registry; undo it
    return rt


def load_pre2_snapshot(
    exe_path: str | Path,
    snapshot_dir: str | Path,
    *,
    game_root: str | Path | None = None,
    fast_adlib: bool = False,
    native_replacements: bool = True,
) -> Runtime:
    rt = load_dos_snapshot(
        resolve_pre2_exe_path(exe_path),
        snapshot_dir,
        game_root=game_root,
    )
    _install_bootstrap_helpers(rt)
    if fast_adlib:
        install_fast_adlib_service(rt)
    if native_replacements:
        install_pre2_replacements(rt)
    else:
        uninstall_pre2_replacements(rt)  # dos_re auto-installs the registry; undo it
    return rt
