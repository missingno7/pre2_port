"""TEMPORARY probe — in-VM lockstep verify of the recovered grid redraw (35A1).

Replays gameplay with all hybrid hooks UNINSTALLED (pure ASM oracle: its own 35A1
body + 3B88 blit). At each 35A1 call we snapshot the planes + inputs, run the
recovered ``draw_grid`` on the snapshot, let the ASM run to its RET, then diff: the
four EGA planes and the caller-visible memory side effects — [0x2DF2] tile flags,
[0x2DF4] dirty, [0x2DF5] dirty-rows, and prev camera [0x2DE0]/[0x2DE2] — plus that
``di`` is preserved. Covers both the early-exit and redraw paths. Zero divergence.

Retire when: a headless 35A1 lockstep is folded into the test suite.
Run:  python -m pre2.probes.verify_grid
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.bridge import sprites as spr
from pre2.bridge.frame import (
    DATA_SEG, read_blit_type_table, read_mask_region, read_tilemap,
)
from pre2.checkpoints import uninstall_pre2_replacements
from pre2.recovered.frame_renderer import draw_grid
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
GRID = (0x1030, 0x35A1)
LIMIT = 200

V = {"cam_x": 0x2DE0, "cam_y": 0x2DE2, "prev_x": 0x2DE0, "prev_y": 0x2DE2,
     "col_ring": 0x2DE4, "scroll_src": 0x2DB6, "fine": 0x6BC4,
     "tile_flags": 0x2DF2, "dirty": 0x2DF4, "dirty_rows": 0x2DF5}


def _rb(mem, off):
    return mem.data[((DATA_SEG << 4) + off) & 0xFFFFF]


def _rw(mem, off):
    b = ((DATA_SEG << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def main() -> int:
    playback = InputDemoPlayback.load(DEMO)
    meta = playback.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(
        ROOT / "assets" / "pre2.exe", playback.snapshot_path(),
        game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)),
    )
    cpu = rt.cpu
    cpu.trace_enabled = False
    uninstall_pre2_replacements(rt)

    state = {"redrew": 0, "exited": 0}
    diverged: list[str] = []

    def _run_to_return(c):
        entry_sp = c.s.sp & 0xFFFF
        fn = c.replacement_hooks.pop(GRID, None)
        nm = c.hook_names.pop(GRID, None)
        try:
            for _ in range(4_000_000):
                c.step()
                if (c.s.sp & 0xFFFF) > entry_sp:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[GRID] = fn
            if nm is not None:
                c.hook_names[GRID] = nm

    def handler(c):
        mem = c.mem
        entry_di = c.s.di & 0xFFFF
        pre = {k: (_rb(mem, V[k]) if k in ("col_ring", "fine", "tile_flags", "dirty", "dirty_rows")
                   else _rw(mem, V[k])) for k in V}
        tilemap = read_tilemap(mem)
        blit_type = read_blit_type_table(mem)
        mask_region = read_mask_region(mem)

        snap = spr.snapshot_planes(mem)
        try:
            res = draw_grid(snap, tilemap, pre["cam_x"], pre["cam_y"], pre["prev_x"], pre["prev_y"],
                            pre["dirty"], pre["dirty_rows"], pre["scroll_src"], pre["col_ring"],
                            pre["fine"], blit_type, mask_region)
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to_return(c)
            return

        _run_to_return(c)

        # expected memory after: prev always written; flags only on redraw.
        exp_dee = res.tile_flags if res.redrew else pre["tile_flags"]
        exp_df0 = res.dirty if res.redrew else pre["dirty"]
        exp_df1 = res.dirty_rows if res.redrew else pre["dirty_rows"]
        reason = None
        if res.redrew:
            for p in range(4):
                if bytes(spr.snapshot_planes(mem)[p]) != bytes(snap[p]):
                    reason = f"plane {p}"
                    break
        byte_checks = (("[0x2DF2]", V["tile_flags"], exp_dee & 0xFF),
                       ("[0x2DF4]", V["dirty"], exp_df0 & 0xFF),
                       ("[0x2DF5]", V["dirty_rows"], exp_df1 & 0xFF))
        word_checks = (("[0x2DE0]", V["prev_x"], res.prev_x & 0xFFFF),
                       ("[0x2DE2]", V["prev_y"], res.prev_y & 0xFFFF))
        if reason is None:
            for name, off, val in byte_checks:
                if _rb(mem, off) != val:
                    reason = f"{name} asm={_rb(mem, off):X} rec={val:X}"
                    break
        if reason is None:
            for name, off, val in word_checks:
                if _rw(mem, off) != val:
                    reason = f"{name} asm={_rw(mem, off):X} rec={val:X}"
                    break
        if reason is None and (c.s.di & 0xFFFF) != entry_di:
            reason = f"di not preserved {c.s.di & 0xFFFF:04X}!={entry_di:04X}"

        if res.redrew:
            state["redrew"] += 1
        else:
            state["exited"] += 1
        if reason is not None:
            diverged.append(f"call#{state['redrew']+state['exited']} "
                            f"redrew={res.redrew} cam=({pre['cam_x']},{pre['cam_y']}): {reason}")
        if state["redrew"] + state["exited"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(GRID, None)
            cpu.hook_names.pop(GRID, None)

    cpu.replacement_hooks[GRID] = handler
    cpu.hook_names[GRID] = "frame_grid_verify"

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if GRID not in cpu.replacement_hooks:
            break
        frame += 1

    print(f"grid redraws verified={state['redrew']} early-exits verified={state['exited']}")
    print(f"divergences={diverged[:10]}")
    ok = not diverged and state["redrew"] > 0
    print("FRAME GRID-REDRAW LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
