"""Throwaway witness: lockstep the recovered scene present (1030:9613..9639) vs the ASM.

The mode-select scroll loop (1030:9600) pans the CRTC start + flips the page bookkeeping
every step. This hooks the loop top (9613), captures the inputs (scroll_x [0xB19D],
scroll_y [0xB19F], old page_draw [0xB1A1]), drives the real ASM through the pan + page
flip to 963D, and compares the recovered present_pan_flip against the ASM's CRTC display
start (mem.ega_display_start) + [0xB1A1]/[0xB1A3]. Reached by replaying the menu demo.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.recovered.present import present_pan_flip, pixel_pan
from pre2.runtime import load_pre2_snapshot

ROOT = Path(__file__).resolve().parents[2]
CS, ENTRY, AFTER = 0x1030, 0x9613, 0x963D
_DS = 0x1A0F
SCROLL_X, SCROLL_Y, PAGE_DRAW, PAGE_CLEAR = 0xB19D, 0xB19F, 0xB1A1, 0xB1A3
DEMO = ROOT / "artifacts" / "demo_pre2_20260622_192206"


def _rw(mem, off):
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def main(max_calls=300, max_frames=4000):
    pb = InputDemoPlayback.load(DEMO)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", pb.snapshot_path(),
                            game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)))
    cpu = rt.cpu
    cpu.trace_enabled = False
    s = cpu.s
    res = {"calls": 0, "ok": 0, "bad": 0}
    samples = []

    def probe(c):
        mem = c.mem
        sx, sy, old_draw = _rw(mem, SCROLL_X), _rw(mem, SCROLL_Y), _rw(mem, PAGE_DRAW)
        ds_rec, pd_rec, pc_rec = present_pan_flip(sx, sy, old_draw)
        pan_rec = pixel_pan(sx)
        steps = 0
        while not (s.cs == CS and s.ip == AFTER):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 100_000:
                res["bad"] += 1
                return
        ds_asm = mem.ega_display_start & 0xFFFF
        pd_asm, pc_asm = _rw(mem, PAGE_DRAW), _rw(mem, PAGE_CLEAR)
        ok = (ds_rec == ds_asm and pd_rec == pd_asm and pc_rec == pc_asm)
        res["calls"] += 1
        res["ok" if ok else "bad"] += 1
        if not ok and len(samples) < 8:
            samples.append(dict(sx=sx, sy=sy, ds=(hex(ds_rec), hex(ds_asm)),
                                pd=(hex(pd_rec), hex(pd_asm)), pc=(hex(pc_rec), hex(pc_asm))))

    cpu.replacement_hooks[(CS, ENTRY)] = probe
    cpu.hook_names[(CS, ENTRY)] = "probe:present"

    for f in range(max_frames):
        try:
            pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if res["calls"] >= max_calls:
            break

    print(f"PRESENT pan+flip: calls={res['calls']} ok={res['ok']} bad={res['bad']}")
    for i, sm in enumerate(samples):
        print(f"  [{i}] {sm}")
    return res


if __name__ == "__main__":
    main()
