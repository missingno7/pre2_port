"""Capture a PIXEL witness of the menu-idle attract ANIMATION (title routine 8f2d..9046).

Loads the pre-attract snapshot, runs the VM idle (the attract triggers on the menu-idle
timeout), and at each per-frame present inside the animation routine captures the rendered
page as RGB + the object-slot region. This is the pixel oracle for the native attract-title
animation recovery (task #30).

    python -m pre2.probes.capture_attract_pixels [max_instr]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE              # noqa: E402
from pre2.runtime import load_pre2_snapshot                          # noqa: E402
from sdl_view import render_planar_rgb_from_planes                   # noqa: E402

DS = 0x1A0F
DS_BASE = DS << 4
SNAP = "artifacts/snapshot_pre2_20260703_090150"
ANIM_ENTRY, ANIM_RET = 0x8F2D, 0x9046
PRESENT_IPS = {0x8FAA, 0x903C}   # per-frame present return points (phase1 / phase2)


def _page_planes(mem, page):
    return [bytes(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE + page:
                           EGA_APERTURE + p * EGA_PLANE_STRIDE + page + 0x4000]) for p in range(4)]


def main():
    max_instr = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000_000
    out = ROOT / "artifacts" / "attract_pixels"
    out.mkdir(parents=True, exist_ok=True)
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), str(ROOT / SNAP),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    st = {"in": False, "frames": 0, "count": 0}
    recs = []
    orig = cpu.step

    def sstep():
        s = cpu.s
        if (s.cs & 0xFFFF) == 0x1030:      # NB: no DS gate — at the present IP (8FAA) DS is transiently
            ip = s.ip & 0xFFFF             # 0xA000 (left by the 9071/26FA blits); writes below are absolute
            if ip == 0x8E90:                       # the idle-counter compare; timer ISR is dead in
                mem.data[DS_BASE + 0x27F0] = 0x0F  # this harness, so force past threshold 0x10E -> attract
                mem.data[DS_BASE + 0x27F1] = 0x01
            if ip == 0x1C6F:                       # frame-pace wait: |[27ee]-cs:[1d67]|>=3. ISR dead ->
                v = (mem.data[DS_BASE + 0x27EE] | (mem.data[DS_BASE + 0x27EF] << 8)) + 4   # tick it ourselves
                mem.data[DS_BASE + 0x27EE] = v & 0xFF
                mem.data[DS_BASE + 0x27EF] = (v >> 8) & 0xFF
            if ip == 0x8FA7 and "base" not in st:   # after 9071 restores bg, before 26FA draws sprites:
                page = mem.data[DS_BASE + 0x2DD8] | (mem.data[DS_BASE + 0x2DD9] << 8)
                planes = _page_planes(mem, page)
                import pickle
                (out / "base_planes.pkl").write_bytes(pickle.dumps(
                    {"planes": planes, "page": page, "pal": list(rt.dos.vga_palette)}))
                (out / "frame0_mem.bin").write_bytes(bytes(mem.data))   # full 1MB for offline native render
                st["base"] = True
                print(f"saved pristine base planes (page={page:04x}) + frame0_mem.bin")
            if ip == ANIM_ENTRY:
                st["in"] = True
            elif ip == ANIM_RET:
                st["in"] = False; st["done"] = True
            elif st["in"] and ip in PRESENT_IPS and st["frames"] < 120:
                page = mem.data[DS_BASE + 0x2DD8] | (mem.data[DS_BASE + 0x2DD9] << 8)
                disp = rt.program.memory.ega_display_start
                pal = list(rt.dos.vga_palette)
                rgb = np.asarray(render_planar_rgb_from_planes(_page_planes(mem, page), 0, pal), np.uint8)
                Image.fromarray(rgb).save(out / f"anim_{st['frames']:03d}.png")
                slots = {o: mem.data[DS_BASE + o] for o in range(0x4F1C, 0x4F70)}
                recs.append({"frame": st["frames"], "ip": ip, "page": page,
                             "cav_x": slots.get(0x4F1C, 0) | (slots.get(0x4F1D, 0) << 8),
                             "cav_sid": slots.get(0x4F20, 0) | (slots.get(0x4F21, 0) << 8)})
                st["frames"] += 1
                print(f"frame {st['frames']-1} ip={ip:04x} page={page:04x} "
                      f"cav_x={recs[-1]['cav_x']} sid={recs[-1]['cav_sid']:04x}")
        return orig()

    cpu.step = sstep
    for _ in range(max_instr):
        cpu.step()
        if st.get("done") or st["frames"] >= 120:
            break
    print(f"captured {st['frames']} frames to {out}")
    import json
    (out / "recs.json").write_text(json.dumps(recs, indent=1))


if __name__ == "__main__":
    main()
