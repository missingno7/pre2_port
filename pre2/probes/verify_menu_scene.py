"""Verify the recovered MenuScenePage reproduces the VM's menu A000 page byte-exact.

Replays the menu-navigating demo with PURE ASM (the authoritative producer), feeds the SAME leaf-call
events the controller performs into a recovered MenuScenePage (seed at the 9718 fill / 9725; draw_string
stamps at 9886; scroll_shift at 9804), and diffs the owned planes vs the VM VRAM at each menu frame
(9877, the scroll_shift block exit). diff=0 proves the stateful persistent-page model.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge import present as PB
from pre2.bridge import text as TB
from pre2.recovered.menu_scene import MenuScenePage
from pre2.runtime import load_pre2_snapshot

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "artifacts" / "demo_pre2_20260622_192206"
CS = 0x1030
_SEED, _TEXT, _SHIFT, _SHIFT_EXIT, _MENU_EXIT = 0x9725, 0x9886, 0x9804, 0x9877, 0x9885


def _vm_plane(d, p, n=0x2000):
    a = EGA_APERTURE + p * EGA_PLANE_STRIDE
    return d[a:a + n]


def main(max_frames=4000, max_checks=400):
    pb = InputDemoPlayback.load(DEMO)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", pb.snapshot_path(),
                            game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)),
                            native_replacements=False)        # PURE ASM oracle
    cpu = rt.cpu
    d = rt.program.memory.data
    page = MenuScenePage()
    state = {"active": False, "checks": 0, "bad": 0, "first_bad": None}

    def w(off):
        b = (0x1A0F << 4) + off
        return d[b] | (d[b + 1] << 8)

    def at_seed(c):
        seg = w(0x2875)
        asset = bytes(d[seg << 4:(seg << 4) + 0x4000])
        page.seed(asset)
        state["active"] = True
        interpret_current_instruction_without_hook(c)

    def at_text(c):
        if state["active"]:
            ti = TB.read_text_inputs(c.mem, c.s.ds, c.s.bx)
            page.stamp_text(ti.text, ti.font, ti.font_base, ti.pen, ti.advance, ti.page_draw, ti.page_clear)
        interpret_current_instruction_without_hook(c)

    def at_shift(c):
        if state["active"]:
            b199, sx, sy, psy, pd = PB.read_scroll_shift_inputs(c.mem)
            page.scroll_shift(b199, sx, sy, psy, pd, wrap=c.s.bp)
        interpret_current_instruction_without_hook(c)

    def at_shift_exit(c):
        if state["active"] and state["checks"] < max_checks:
            state["checks"] += 1
            for p in range(4):
                if page.planes[p][:0x2000] != _vm_plane(d, p):
                    state["bad"] += 1
                    if state["first_bad"] is None:
                        i = next(k for k in range(0x2000) if page.planes[p][k] != _vm_plane(d, p)[k])
                        state["first_bad"] = (state["checks"], p, hex(i),
                                              page.planes[p][i], _vm_plane(d, p)[i])
                    break
        interpret_current_instruction_without_hook(c)

    def at_menu_exit(c):
        state["active"] = False
        interpret_current_instruction_without_hook(c)

    for ip, fn in ((_SEED, at_seed), (_TEXT, at_text), (_SHIFT, at_shift),
                   (_SHIFT_EXIT, at_shift_exit), (_MENU_EXIT, at_menu_exit)):
        cpu.replacement_hooks[(CS, ip)] = fn
        cpu.hook_names[(CS, ip)] = f"menuscene_{ip:04x}"

    for f in range(max_frames):
        try:
            pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if state["checks"] >= max_checks:
            break

    print(f"MENU SCENE: frames-checked={state['checks']} bad={state['bad']} first_bad={state['first_bad']}")
    return state


if __name__ == "__main__":
    main()
