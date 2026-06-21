"""Prehistorik 2 runtime construction on top of the reusable DOS VM."""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import install_lzexe_0069_accelerator
from dos_re.runtime import Runtime, create_runtime as create_dos_runtime
from dos_re.snapshot import load_snapshot as load_dos_snapshot
from pre2.bootstrap_hooks import install_fast_adlib_service
from pre2.checkpoints import install_pre2_replacements, uninstall_pre2_replacements

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


# A distinctive byte run from the draw-list interpreter at CS:0x52E2
# (`push es; push ds; mov es,[2DD6]; mov ds,[2871]; xor si,si; mov di,[si]`),
# used to locate PRE2 inside a DOSBox memory image (DOSBox loads it at a different
# segment than our VM).  The two PRE2 layout constants are the paragraph distance
# from code to data (DGROUP) and from code to the runtime stack segment.
_PRE2_CODE_SIG = bytes.fromhex("061e8e06d62d8e1e712833f68b3c")
_PRE2_CODE_SIG_OFF = 0x52E2
_PRE2_DATA_FROM_CODE = 0x9E3   # data_seg - code_seg  (1A0F - 1030)
_PRE2_STACK_FROM_CODE = -0x20  # stack_seg - code_seg (1010 - 1030)


def load_dosbox_savestate(
    exe_path: str | Path,
    sav_path: str | Path,
    *,
    game_root: str | Path | None = None,
    fast_adlib: bool = False,
    native_replacements: bool = False,
) -> Runtime:
    """Start a runtime from a DOSBox-X ``.sav`` as if it were one of our snapshots.

    DOSBox loads PRE2 at a different segment than our VM, so we (a) drop its
    conventional 1 MB into our memory verbatim, (b) locate the program by a code
    signature to recover CS, and derive DS/ES/SS from PRE2's fixed layout, and
    (c) load the general registers / IP / flags from the save's CPU component.

    Native (recovered) hooks default OFF: they are keyed to our load segment
    (0x1030) so they would not match DOSBox's layout — the game runs as pure ASM,
    which is exactly what we want for a reference comparison.
    """
    from dos_re.cpu import CPUState
    from dos_re.dosbox_savestate import (
        conventional_memory, locate_conventional, read_gpr, read_memory_image,
    )

    image = read_memory_image(sav_path)
    header_len, cs = locate_conventional(image, _PRE2_CODE_SIG, _PRE2_CODE_SIG_OFF)
    conv = conventional_memory(image, header_len)
    gpr = read_gpr(sav_path)
    ds = (cs + _PRE2_DATA_FROM_CODE) & 0xFFFF
    ss = (cs + _PRE2_STACK_FROM_CODE) & 0xFFFF

    rt = create_pre2_runtime(
        exe_path, game_root=game_root, fast_adlib=fast_adlib,
        native_replacements=native_replacements,
    )
    rt.program.memory.data[:len(conv)] = conv
    rt.cpu.mem = rt.program.memory
    rt.cpu.s = CPUState(
        cs=cs, ip=gpr["ip"], ss=ss, sp=gpr["sp"], ds=ds, es=ds,
        ax=gpr["ax"], bx=gpr["bx"], cx=gpr["cx"], dx=gpr["dx"],
        si=gpr["si"], di=gpr["di"], bp=gpr["bp"], flags=gpr["flags"],
    )
    # PRE2 runs the tally/level in 16-colour planar mode 0Dh.
    rt.dos.video_mode = 0x0D
    rt.program.memory.ega_planar = True
    return rt
