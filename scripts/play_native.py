"""Play a Prehistorik 2 level with the VM-LESS native core.

This is the standalone gameplay runner: it seeds a NativeGameState from the game's assets (via the recovered
``native_level_load`` over a snapshot base for the surrounding assets/palette), drops the player at the level
start, and then runs the *recovered* gameplay every frame — player FSM, object system, camera follow — with NO
emulator in the loop. Your keyboard drives it; ``native_render`` draws it.

    python scripts/play_native.py            # play LEVEL3 (the snapshot's level)
    python scripts/play_native.py --level 2  # internal level index (0-based; 2 -> LEVEL3.SQZ)

Controls: arrow keys / numpad = move, SPACE = fire/jump, ESC = quit.

(The boot/front-end still uses the EXE once to bootstrap the surrounding assets; the *gameplay* is entirely the
recovered native code over a NativeGameState. The cold-boot-from-files + native front-end are tracked separately.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

DS = 0x1A0F << 4
_DEFAULT_SNAPSHOT = "artifacts/demo_pre2_full_gorilla_20260628_203423"


def _ww(data, off, val):
    data[DS + off] = val & 0xFF
    data[DS + off + 1] = (val >> 8) & 0xFF


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Play a PRE2 level with the VM-less native core")
    ap.add_argument("--level", type=int, default=2, help="internal level index (0-based; 2 -> LEVEL3)")
    ap.add_argument("--snapshot", default=_DEFAULT_SNAPSHOT, help="snapshot dir to bootstrap surrounding assets")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args(argv)

    import numpy as np
    import pygame
    from dos_re.input_demo import InputDemoPlayback
    from pre2.runtime import load_pre2_snapshot
    from pre2.native.state import NativeGameState
    from pre2.native.render import native_render
    from pre2.native.runtime import native_frame_step
    from pre2.native.level_init import native_level_init
    from pre2.native.input import apply_input
    from sdl_view import render_planar_rgb_from_planes
    import play

    gr = str(ROOT / "assets")
    print(f"Booting LEVEL{args.level + 1} (bootstrap from {args.snapshot}, then VM-less native gameplay)...")
    pb = InputDemoPlayback.load(str(ROOT / args.snapshot))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(), game_root=gr,
                            native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False
    det = lambda: cpu.instruction_count / (2142 * 70); rt.dos.time_source = det; tick = {"next": 0.0}
    for _ in range(30):                                              # warm up into a stable gameplay frame
        play._advance_demo_frame(rt, chunk_steps=2142, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=None, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)

    dos = rt.dos
    disp = rt.program.memory.ega_display_start

    # --- seed a NativeGameState via the faithful VM-less level-init (load + re-init + player + centred camera,
    #     every leaf byte-exact vs the ASM), VM-less from here on ---
    state = NativeGameState(bytearray(cpu.mem.data))
    state.data[DS + 0x2D8A] = args.level                            # select the level
    native_level_init(state, game_root=gr)
    state.data[DS + 0x27F4:DS + 0x27F4 + 0x90] = b"\x00" * 0x90      # clear residual input

    print("Ready — arrow keys / numpad = move, SPACE = fire/jump, ESC = quit. (VM-less native gameplay)")
    pygame.init()
    screen = pygame.display.set_mode((320 * args.scale, 200 * args.scale))
    pygame.display.set_caption(f"PRE2 — VM-less native gameplay (LEVEL{args.level + 1})")
    clock = pygame.time.Clock()
    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                running = False
        k = pygame.key.get_pressed()
        apply_input(state,
                    left=k[pygame.K_LEFT] or k[pygame.K_KP4],
                    right=k[pygame.K_RIGHT] or k[pygame.K_KP6],
                    up=k[pygame.K_UP] or k[pygame.K_KP8],
                    down=k[pygame.K_DOWN] or k[pygame.K_KP2],
                    fire=k[pygame.K_SPACE])
        try:
            planes, page = native_frame_step(state, dos, disp, game_root=gr)
        except Exception as e:                                       # never crash the window on a gap
            planes, page = native_render(state, dos, disp, game_root=gr)
        rgb = np.asarray(render_planar_rgb_from_planes(planes, page, dos.vga_palette), np.uint8)
        surf = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))     # (200,320,3) -> surface
        screen.blit(pygame.transform.scale(surf, screen.get_size()), (0, 0))
        pygame.display.flip()
        clock.tick(70)
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
