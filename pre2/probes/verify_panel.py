"""TEMPORARY probe — in-VM lockstep verify of the recovered page-flip copy (3054).

Pure-ASM oracle (hooks uninstalled). At each 3054 call: snapshot planes, run the
recovered ``panel_copy`` on the snapshot, let the ASM run to its RET, diff the four
EGA planes (the only output; the vsync wait is timing-only and registers are
preserved). Zero divergence.

Retire when: a headless 3054 lockstep is folded into the test suite.
Run:  python -m pre2.probes.verify_panel
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.bridge import sprites as spr
from pre2.bridge.frame import DATA_SEG
from pre2.checkpoints import uninstall_pre2_replacements
from pre2.recovered.frame_renderer import panel_copy
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
PANEL = (0x1030, 0x3054)
LIMIT = 50


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

    state = {"n": 0}
    diverged: list[str] = []

    def _run_to_return(c):
        entry_sp = c.s.sp & 0xFFFF
        fn = c.replacement_hooks.pop(PANEL, None)
        nm = c.hook_names.pop(PANEL, None)
        try:
            for _ in range(4_000_000):
                c.step()
                if (c.s.sp & 0xFFFF) > entry_sp:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[PANEL] = fn
            if nm is not None:
                c.hook_names[PANEL] = nm

    def handler(c):
        mem = c.mem
        src_page, dst_page = _rw(mem, 0x2DD4), _rw(mem, 0x2DD2)
        snap = spr.snapshot_planes(mem)
        try:
            panel_copy(snap, src_page, dst_page)
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to_return(c)
            return

        _run_to_return(c)

        reason = None
        live = spr.snapshot_planes(mem)
        for p in range(4):
            if bytes(live[p]) != bytes(snap[p]):
                a, b = bytes(live[p]), bytes(snap[p])
                i = next(k for k in range(len(a)) if a[k] != b[k])
                reason = f"plane {p} @ {i:#06x}: asm={a[i]:02X} rec={b[i]:02X}"
                break

        state["n"] += 1
        if reason is not None:
            diverged.append(f"call#{state['n']} src={src_page:#x} dst={dst_page:#x}: {reason}")
        if state["n"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(PANEL, None)
            cpu.hook_names.pop(PANEL, None)

    cpu.replacement_hooks[PANEL] = handler
    cpu.hook_names[PANEL] = "panel_copy_verify"

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if PANEL not in cpu.replacement_hooks:
            break
        frame += 1

    print(f"panel copies verified={state['n']}")
    print(f"divergences={diverged[:6]}")
    ok = not diverged and state["n"] > 0
    print("PANEL-COPY LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
