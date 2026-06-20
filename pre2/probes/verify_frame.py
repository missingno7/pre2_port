"""TEMPORARY probe — in-VM lockstep verify of the recovered tile-row draw (346E).

Replays gameplay with all hybrid hooks UNINSTALLED, so the original ASM is a pure
independent oracle (its own 346E body + its own 3B69 blit). At each 346E call we
snapshot the planes + inputs, run the recovered ``draw_tile_row`` (which composes
the recovered ``blit_sprite``) on the snapshot, let the ASM run the routine to its
RET, then diff: the four EGA planes, the exit ``di``, and the three OR-accumulated
flag bytes ([0x6BB9]/[0x2DEE]/[0x2DF0]). Asserts zero divergence.

This is the verification contract for ``pre2/recovered/frame_renderer.py``.

Run:  python -m pre2.probes.verify_frame
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
from pre2.recovered.frame_renderer import RowFlags, draw_tile_row
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
ROW_DRAW = (0x1030, 0x346E)
LIMIT = 200  # cap on verified row-draws (346E fires several times per scroll step)

VAR_PLANE_ATTR = 0x6BB9
VAR_TILE_FLAGS = 0x2DEE
VAR_TILE_TYPE = 0x2DF0


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
    uninstall_pre2_replacements(rt)  # pure-ASM oracle

    state = {"verified": 0}
    diverged: list[str] = []

    def _run_to_return(c):
        entry_sp = c.s.sp & 0xFFFF
        fn = c.replacement_hooks.pop(ROW_DRAW, None)
        nm = c.hook_names.pop(ROW_DRAW, None)
        try:
            for _ in range(2_000_000):
                c.step()
                if (c.s.sp & 0xFFFF) > entry_sp:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[ROW_DRAW] = fn
            if nm is not None:
                c.hook_names[ROW_DRAW] = nm

    def handler(c):
        mem = c.mem
        # --- capture inputs (346E has not executed yet) ---
        tile_offset = c.s.ax & 0xFFFF
        di = c.s.di & 0xFFFF
        scroll_src = _rw(mem, 0x2DB6)
        col_ring = _rb(mem, 0x2DE4)
        fine_scroll = _rb(mem, 0x6BC0)
        tilemap = read_tilemap(mem)
        blit_type = read_blit_type_table(mem)
        mask_region = read_mask_region(mem)
        seed = RowFlags(_rb(mem, VAR_PLANE_ATTR), _rb(mem, VAR_TILE_FLAGS), _rb(mem, VAR_TILE_TYPE))

        # --- recovered prediction on a detached plane snapshot ---
        snap = spr.snapshot_planes(mem)
        try:
            pred_di, pred_flags = draw_tile_row(
                snap, tilemap, tile_offset, di, scroll_src, col_ring,
                fine_scroll, blit_type, mask_region, seed,
            )
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to_return(c)
            return

        # --- ASM oracle ---
        _run_to_return(c)

        # --- diff contract ---
        reason = None
        asm_planes = spr.snapshot_planes(mem)
        for p in range(4):
            if bytes(asm_planes[p]) != bytes(snap[p]):
                # locate first differing byte for a useful message
                a, b = bytes(asm_planes[p]), bytes(snap[p])
                i = next(k for k in range(len(a)) if a[k] != b[k])
                reason = f"plane {p} @ {i:#06x}: asm={a[i]:02X} rec={b[i]:02X}"
                break
        # 346E pushes/pops di (3471/34E8): the caller's di is preserved; the
        # internal pred_di is scratch (not part of the contract).
        if reason is None and (c.s.di & 0xFFFF) != di:
            reason = f"di not preserved: entry={di:04X} asm_exit={c.s.di & 0xFFFF:04X}"
        _ = pred_di
        if reason is None:
            for name, off, val in (("plane_attr", VAR_PLANE_ATTR, pred_flags.plane_attr & 0xFF),
                                   ("tile_flags", VAR_TILE_FLAGS, pred_flags.tile_flags & 0xFF),
                                   ("tile_type", VAR_TILE_TYPE, pred_flags.tile_type & 0xFF)):
                if _rb(mem, off) != val:
                    reason = f"{name} asm={_rb(mem, off):02X} rec={val:02X}"
                    break

        state["verified"] += 1
        if reason is not None:
            diverged.append(f"row#{state['verified']} off={tile_offset:04X} di={di:04X}: {reason}")
        if state["verified"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(ROW_DRAW, None)
            cpu.hook_names.pop(ROW_DRAW, None)

    cpu.replacement_hooks[ROW_DRAW] = handler
    cpu.hook_names[ROW_DRAW] = "frame_row_verify"

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if ROW_DRAW not in cpu.replacement_hooks:
            break
        frame += 1

    print(f"tile-row draws verified={state['verified']}")
    print(f"divergences={diverged[:10]}")
    ok = not diverged and state["verified"] > 0
    print("FRAME ROW-DRAW LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
