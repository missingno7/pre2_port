"""In-VM lockstep verify of the consolidated renderer seam ``render_frame``.

Proves that the recovered, VM-independent ``render_frame(RendererState)`` reproduces the
original renderer's output **standalone (no VM stepping)**: it drives a gameplay snapshot
with the pure ASM as the oracle, and at each animated-grid redraw it

  1. captures ``RendererState`` from the frozen memory image (the bridge, read-only) at the
     grid-loop entry ``36B3`` — the post-controller instant, after the camera + animation
     frame ``[0x6BC2]`` advance — plus a snapshot of the four EGA planes;
  2. lets the ASM run animgrid + grid to the scroll entry ``3A27``;
  3. runs ``render_frame`` on the captured state + planes (off the VM);
  4. diffs the **renderer-owned background ring buffer** (``0x3F40..0x5E00``, which animgrid
     + grid produce) byte-for-byte.

This is the repeatable verification contract for ``pre2/recovered/render_frame.py`` (the
drop-in seam for a future native enhanced renderer). The visible screen pages are *not*
compared: the object system (``65A0``/``8BFF``) layers gameplay sprites there via the shared
blit — the documented border, outside the renderer.

Needs a gameplay snapshot (gitignored, legally-owned GOG copy). Movement is injected so the
camera scrolls (forcing grid redraws), since static forward-runs do not scroll.

Run:  python -m pre2.probes.verify_render_frame [artifacts/snapshot_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import play
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_frame import render_frame
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
ANIM_GRID_LOOP = 0x36B3   # grid-loop entry (throttle ran; [0x6BC2] advanced)
SCROLL_ENTRY = 0x3A27     # after animgrid + grid, before the scroll copy
BG_LO, BG_HI = 0x3F40, 0x5E00   # the scrolling tile ring buffer (renderer-owned)
DEFAULT_SNAP = ROOT / "artifacts" / "snapshot_pre2_gameplay_20260621_185902"
LIMIT = 12


class _Frozen:
    """A read-only memory image (``.data``) for the bridge readers — no VM stepping."""
    def __init__(self, data):
        self.data = data


def _planes(mem):
    return [bytearray(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE:
                               EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]) for p in range(4)]


def main() -> int:
    snap_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SNAP
    if not snap_dir.exists():
        print(f"snapshot not found: {snap_dir} (needs a gameplay snapshot under artifacts/)")
        return 2
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", snap_dir,
                            game_root=ROOT / "assets", native_replacements=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt)
    sb.clock = perf_counter

    state = {"frozen": None, "planes": None}
    verified = {"n": 0}
    diverged: list[str] = []

    def at_grid_loop(c):
        state["frozen"] = _Frozen(bytearray(c.mem.data))
        state["planes"] = _planes(c.mem)
        interpret_current_instruction_without_hook(c)

    def at_scroll(c):
        if state["frozen"] is not None:
            asm = _planes(c.mem)
            rs = read_renderer_state(state["frozen"])
            pl = [bytearray(p) for p in state["planes"]]
            render_frame(rs, pl, None)
            verified["n"] += 1
            for p in range(4):
                for off in range(BG_LO, BG_HI):
                    if pl[p][off] != asm[p][off]:
                        diverged.append(f"frame#{verified['n']} plane{p}@{off:#06x}: "
                                        f"asm={asm[p][off]:02X} rec={pl[p][off]:02X}")
                        break
                if diverged:
                    break
            state["frozen"] = None
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(CS, ANIM_GRID_LOOP)] = at_grid_loop
    cpu.hook_names[(CS, ANIM_GRID_LOOP)] = "render_frame_capture"
    cpu.replacement_hooks[(CS, SCROLL_ENTRY)] = at_scroll
    cpu.hook_names[(CS, SCROLL_ENTRY)] = "render_frame_verify"

    ts = {"next": perf_counter()}
    rt.dos.time_source = perf_counter
    for i in range(160):
        if i % 3 == 0:
            try:
                deliver_scancode(rt, 0x4D if (i // 30) % 2 == 0 else 0x4B, max_steps=2_000_000)
            except Exception:  # noqa: BLE001
                pass
        play._pump_and_step(rt, now=perf_counter(), pic=rt.dos.pic, sound_blaster=sb,
                            timer_irq=True, input_irq_steps=2_000_000, tick_state=ts, n_steps=2000)
        if verified["n"] >= LIMIT or diverged:
            break

    print(f"render_frame bg-buffer frames verified={verified['n']}")
    print(f"divergences={diverged[:10]}")
    ok = not diverged and verified["n"] > 0
    print("RENDER_FRAME STANDALONE LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
