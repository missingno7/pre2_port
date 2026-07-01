"""VM-less cold boot — assemble a NativeGameState from the game files alone, with no emulator.

THE ENDGAME RUNTIME ([[pre2-hybrid-to-native-model]]): fully VM-less and ASM-less. At runtime nothing
interprets x86 and no ``PRE2.EXE`` is executed — the game loads a pre-extracted STATIC BOOT IMAGE (the
EXE's initialized memory, a plain data blob) plus the GOG asset files (``*.SQZ``/``*.TRK``) and runs the
recovered Python game logic. Copy the package + the boot image + the assets to any folder and run it.

The boot image is produced ONCE by :func:`build_boot_image` — the VM unpacks the EXE there, which is its
ONLY role (a build/oracle tool, never a runtime dependency, exactly as the north star intends). PRE2's
packer is custom/multi-stage (not stock LZEXE — the decompression loop isn't even present in the file),
so a pure-Python runtime unpacker would be a separate reverse-engineering effort; the one-time VM
extraction sidesteps it while keeping the SHIPPED runtime VM-less.

Proven: `native_cold_boot` -> L1 renders byte-identical to the VM-extracted-image path (0 px diff).
"""
from __future__ import annotations

import pathlib
import zlib

from pre2.native.input import init_keyboard_input
from pre2.native.level_init import native_level_init
from pre2.native.sprite_bank import native_build_sprite_bank
from pre2.native.state import NativeGameState

_DS = 0x1A0F << 4
_LEVEL_SEL = 0x2D8A                  # [asm] the selected level (0-based)
# Capture at the OLDIES scene entry (1030:240A) — the game's FIRST visible screen — NOT at main entry (1030:00A0).
# main() runs its boot init between them (loads KEYB/ALLFONTS fonts -> font seg [0x3D], the joystick detect ->
# [0x27E4], the year [0x37], palette), which the whole front-end (OLDIES credits, menu/map text) needs. Capturing
# at 240A carries all of it; capturing at 00A0 gave an empty font seg -> OLDIES drew garbage glyphs. 240A is before
# the heavy SAMPLE.SQZ decode (07c9, after OLDIES), so the one-time build stays fast.
_BOOT_CAPTURE = (0x1030, 0x240A)


def build_boot_image(exe_path: str, out_path: str, *, game_root: str | None = None) -> int:
    """[BUILD STEP — the VM's only use] Extract the EXE's static boot image (its initialized memory at the OLDIES
    scene entry ``1030:240A``, after the packer self-unpacks AND ``main`` runs its boot init: fonts, joystick,
    palette) and write it zlib-compressed to ``out_path``. Run once per game; the result is the only thing the
    VM-less runtime needs from ``PRE2.EXE``. Returns the bundle size."""
    from pre2.runtime import create_pre2_runtime
    rt = create_pre2_runtime(exe_path, game_root=game_root, native_replacements=False)
    cpu = rt.cpu
    cpu.trace_enabled = False
    for _ in range(40_000_000):
        if (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF) == _BOOT_CAPTURE:
            break
        cpu.step()
    else:
        raise RuntimeError("never reached the OLDIES entry 1030:240A while extracting the boot image")
    blob = zlib.compress(bytes(cpu.mem.data), 9)
    pathlib.Path(out_path).write_bytes(blob)
    return len(blob)


def load_boot_image(path: str) -> bytearray:
    """[RUNTIME — no VM] Decompress a boot image (built by :func:`build_boot_image`) into a 1 MB memory buffer."""
    return bytearray(zlib.decompress(pathlib.Path(path).read_bytes()))


def native_cold_boot(game_root: str, boot_image_path: str, *, level: int = 0) -> NativeGameState:
    """[RUNTIME — no VM, no EXE] Assemble a :class:`NativeGameState` for ``level`` (0-based) entirely from the
    game files: the static boot image + the recovered native asset/level loaders. Returns a ready state
    (render it with ``native.render`` / step it with ``native.runtime`` — no emulator anywhere)."""
    state = NativeGameState(load_boot_image(boot_image_path))
    native_build_sprite_bank(state, game_root=game_root)          # the shared SPRITES.SQZ 5-plane bank
    init_keyboard_input(state)                                    # boot joystick-detect outcome (keyboard play)
    state.data[_DS + _LEVEL_SEL] = level & 0xFF
    native_level_init(state, game_root=game_root)                 # load + init the level, VM-less
    return state
