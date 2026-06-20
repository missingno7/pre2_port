"""TEMPORARY probe — in-VM lockstep verify of the recovered per-object draw (6544).

Pure-ASM oracle (hooks uninstalled). At each 6544 call: capture inputs (object pos
di, sprite index al, camera, draw mode, blit type/mask/bg), run the recovered
``draw_object_sprite`` on a plane snapshot, let the ASM run to its RET, then diff
the four EGA planes and the drawn/culled decision (the ASM's CF: clear=drawn,
set=culled) + the [0x6BB9]=1 drawn flag. Zero divergence.

Retire when: a headless 6544 lockstep is folded into the test suite.
Run:  python -m pre2.probes.verify_object
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.cpu import CF
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.bridge import frame as _frame
from pre2.bridge import objects as _obj
from pre2.bridge import sprites as _spr
from pre2.bridge.frame import DATA_SEG
from pre2.checkpoints import uninstall_pre2_replacements
from pre2.recovered.object_draw import draw_object_sprite
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
OBJDRAW = (0x1030, 0x6544)
VAR_PLANE_ATTR = 0x6BB9
LIMIT = 80


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

    state = {"drawn": 0, "culled": 0}
    diverged: list[str] = []

    def _run_to_return(c):
        entry_sp = c.s.sp & 0xFFFF
        fn = c.replacement_hooks.pop(OBJDRAW, None)
        nm = c.hook_names.pop(OBJDRAW, None)
        try:
            for _ in range(4_000_000):
                c.step()
                if (c.s.sp & 0xFFFF) > entry_sp:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[OBJDRAW] = fn
            if nm is not None:
                c.hook_names[OBJDRAW] = nm

    def handler(c):
        mem = c.mem
        obj_pos = c.s.di & 0xFFFF
        sprite_index = c.s.ax & 0xFF
        args = (obj_pos, _rw(mem, 0x2DE0), _rw(mem, 0x2DE2), _obj.read_draw_mode(mem),
                sprite_index, _frame.read_blit_type_table(mem), _frame.read_bg_off(mem),
                _frame.read_mask_region(mem))
        attr_before = _rb(mem, VAR_PLANE_ATTR)
        snap = _spr.snapshot_planes(mem)
        try:
            drawn = draw_object_sprite(snap, *args)
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to_return(c)
            return

        _run_to_return(c)

        asm_culled = bool(c.s.flags & CF)            # 6544: stc=culled, clc=drawn
        reason = None
        if drawn == asm_culled:                       # decision must be opposite of CF
            reason = f"decision: recovered drawn={drawn}, asm culled={asm_culled}"
        if reason is None and drawn:
            live = _spr.snapshot_planes(mem)
            for p in range(4):
                if bytes(live[p]) != bytes(snap[p]):
                    reason = f"plane {p}"
                    break
            if reason is None and _rb(mem, VAR_PLANE_ATTR) != 1:
                reason = f"[0x6BB9] drawn flag = {_rb(mem, VAR_PLANE_ATTR)} (want 1)"
        elif reason is None and not drawn:
            if _rb(mem, VAR_PLANE_ATTR) != attr_before:
                reason = "[0x6BB9] changed on a culled object"

        if drawn:
            state["drawn"] += 1
        else:
            state["culled"] += 1
        if reason is not None:
            diverged.append(f"call#{state['drawn']+state['culled']} pos={obj_pos:04X} idx={sprite_index:02X}: {reason}")
        if state["drawn"] + state["culled"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(OBJDRAW, None)
            cpu.hook_names.pop(OBJDRAW, None)

    cpu.replacement_hooks[OBJDRAW] = handler
    cpu.hook_names[OBJDRAW] = "object_draw_verify"

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if OBJDRAW not in cpu.replacement_hooks:
            break
        frame += 1

    print(f"object draws verified: drawn={state['drawn']} culled={state['culled']}")
    print(f"divergences={diverged[:6]}")
    ok = not diverged and (state["drawn"] + state["culled"]) > 0
    print("OBJECT-DRAW LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
