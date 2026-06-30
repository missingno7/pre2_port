"""Play a Prehistorik 2 level with the VM-LESS native core.

The standalone gameplay runner: it bootstraps the EXE-loaded shared assets ONCE from a memory SNAPSHOT (cold
boot-from-files isn't recovered yet), seeds a NativeGameState with the faithful VM-less level-init, then runs the
*recovered* gameplay every frame — player, objects, camera, collision, the boss death/respawn — with NO emulator
in the loop. Your keyboard drives it; ``native_render`` draws it.

    python scripts/play_native.py                 # play LEVEL3
    python scripts/play_native.py --level 1        # internal level index (0-based; 1 -> LEVEL2)
    python scripts/play_native.py --fps 35         # cap the frame rate (game tick is 70Hz; lower = slower)

Controls: arrow keys / numpad = move, SPACE = fire/jump, ESC = quit. The title bar shows the live FPS.

NOTE — the snapshot is NOT a replayed demo. ``--snapshot`` points at a recorded-demo DIRECTORY only because
that's where a usable memory snapshot lives; the demo is never played back — it is just the source of the
EXE-loaded asset state (the shared sprite bank etc. that ``native_level_load`` still defers). The cold-boot-from
-files + native front-end (which would remove the snapshot dependency entirely) are tracked separately (#10/#14).

KNOWN ROUGH EDGES (standalone-runtime gaps, not gameplay): the faithful renderer was built + verified OVER THE VM,
where the ASM render cluster maintains the display-page / smooth-scroll render state each frame; the VM-less
gameplay step does not maintain that render state, so tiles can corrupt once the camera scrolls. Pacing is capped
to ``--fps`` (default = the 70Hz game tick).
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
    ap.add_argument("--snapshot", default=_DEFAULT_SNAPSHOT,
                    help="demo DIR whose memory snapshot bootstraps the EXE-loaded assets (NOT replayed)")
    ap.add_argument("--fps", type=int, default=70, help="frame-rate cap (the game tick is 70Hz; lower runs slower)")
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
    print(f"LEVEL{args.level + 1}: bootstrapping the EXE-loaded assets from the snapshot in '{args.snapshot}' "
          f"(the demo is NOT replayed — just its memory image), then VM-less native gameplay capped at {args.fps} fps...")
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
    frames = 0
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
        clock.tick(args.fps)
        frames += 1
        if frames % 20 == 0:                                         # show the live frame rate
            pygame.display.set_caption(
                f"PRE2 — VM-less native gameplay (LEVEL{args.level + 1})  —  {clock.get_fps():.0f} fps")
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
