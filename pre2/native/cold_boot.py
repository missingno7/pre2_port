"""VM-less cold boot — assemble a NativeGameState from the game files alone, with no emulator and no EXE.

THE ENDGAME RUNTIME ([[pre2-hybrid-to-native-model]]): fully VM-less, ASM-less and EXE-less. The initial
machine state is BOOT CONSTANTS (:mod:`pre2.native.boot_data` — the 64 KB DGROUP initial data segment +
four tiny CS table windows, zeros elsewhere; generated once by ``pre2/probes/extract_boot_data.py``, the
VM's only remaining role). Everything else comes from the GOG asset files (``*.SQZ``/``*.TRK``) via the
recovered native loaders. Copy the package + the game data to any folder and run it.

Proven: the constants-built state is byte-equal to the old VM-extracted boot image over every byte the
runtime reads (map_boot_reads), and the cold-boot golden/pixel tests reproduce identically.
"""
from __future__ import annotations

from pre2.native.assets import load_sqz
from pre2.native.boot_data import build_boot_memory
from pre2.native.input import init_keyboard_input
from pre2.native.level_init import native_level_start
from pre2.native.sprite_bank import native_build_sprite_bank
from pre2.native.state import NativeGameState

_DS = 0x1A0F << 4
_LEVEL_SEL = 0x2D8A                  # [asm] the selected level (0-based)


def native_cold_boot(game_root: str, *, level: int = 0) -> NativeGameState:
    """[RUNTIME — no VM, no EXE] Assemble a :class:`NativeGameState` for ``level`` (0-based) entirely from
    the boot constants + the game files. Returns a ready state (render it with ``native.render`` / step it
    with ``native.runtime`` — no emulator anywhere)."""
    state = NativeGameState(build_boot_memory())
    # main() permanently stacks FRONT.SQZ (the HUD front-panel) into the load buffer BEFORE the sprite bank
    # ([0x2875] 0x27be -> 0x2cd7), so the level then lands at 0x5cc1 — the exact segment the VM loads it at. The
    # boot constants carry the state at OLDIES entry (before this load), so cold-boot must reproduce it: without
    # it the level loads 0x519 paragraphs too low and the parallax over-reads the wrong memory (black sky).
    _front_seg = load_sqz(state, "FRONT.SQZ", game_root=game_root)  # [asm 107B] permanent front-panel load (bumps [0x2875])
    state.data[_DS + 0x3B] = _front_seg & 0xFF                    # [asm 0123] the FOREGROUND tile-gfx bank the 3721 front
    state.data[_DS + 0x3C] = (_front_seg >> 8) & 0xFF            # pass reads (foliage-in-front); missing = no foreground
    native_build_sprite_bank(state, game_root=game_root)          # the shared SPRITES.SQZ 5-plane bank
    _top = state.data[_DS + 0x2875] | (state.data[_DS + 0x2876] << 8)   # [asm 0129-012c] the per-level alloc RESET
    state.data[_DS + 0x39] = _top & 0xFF                          # base ([0x39]=[0x2875] after the front-end assets)
    state.data[_DS + 0x3A] = (_top >> 8) & 0xFF
    from pre2.native.audio import native_load_sfx_bank
    native_load_sfx_bank(state, game_root)                        # [asm 07C9] the SFX sample bank (so effects PLAY)
    init_keyboard_input(state)                                    # boot joystick-detect outcome (keyboard play)
    state.data[_DS + _LEVEL_SEL] = level & 0xFF
    native_level_start(state, game_root=game_root)                # load + init + the level-start block (lives, etc.)
    return state
