"""Witness + lockstep probe for the text leaf ``draw_string`` (``1030:9886``).

The scene island's first leaf needs a *mid-draw* witness: every snapshot we have is captured
*after* the text was drawn (font segment + VGA state gone), so there is no oracle. This drives
a cold boot to the text screens (oldies / title), hooks ``draw_string`` at entry, captures its
inputs + the VGA planes *before* the call, lets the real ASM run to its RET, captures the planes
*after*, then runs the recovered :func:`pre2.recovered.text.draw_string` on the before-image and
diffs planes 2|3 vs the ASM — the byte-exact verification ``text.py`` is pending.

It also dumps the first call's inputs + the ASM's actual write footprint, so the recovered
field layout (string ptr, font base/segment, pen/advance/pages, the page-wrap math) can be
confirmed or corrected against real data.

Run:  python -m pre2.probes.capture_text_draw
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.text import draw_string
from pre2.runtime import create_pre2_runtime

CS = 0x1030
DRAW_STRING = 0x9886
DS = 0x1A0F                      # DGROUP — the text state block lives here
# inferred state-block offsets (DGROUP), from pre2/recovered/text.py
FONT_SEG = 0x2875               # word: font glyph segment
FONT_BASE = 0xB1AC             # word: per-shade glyph base (offset into the font segment)
PEN = 0xB1A6                   # word: starting byte X
ADVANCE = 0xB1AB               # byte: per-char width
PAGE_DRAW = 0xB1A1             # word: draw page offset
PAGE_CLEAR = 0xB1A3            # word: clear page offset


def _rw(mem, seg, off):
    b = ((seg << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _read_planes(mem):
    return [bytearray(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE:
                               EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]) for p in range(4)]


def main() -> int:
    rt = create_pre2_runtime(str(ROOT / "assets" / "pre2.exe"),
                             game_root=str(ROOT / "assets"), fast_adlib=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    pic = enable_sound_blaster(rt) and rt.dos.pic
    cpu.pending_irq = lambda: rt.dos.pic.acknowledge()

    results = {"calls": 0, "verified": 0, "diverged": 0, "incomplete": 0}
    samples = []

    def on_draw_string(c):
        mem = c.mem
        s = c.s
        # --- capture inputs at entry ---
        ds = s.ds
        text_ptr = (ds, s.bx)
        raw = bytes(mem.data[((ds << 4) + s.bx) & 0xFFFFF:((ds << 4) + s.bx + 64) & 0xFFFFF])
        font_seg = _rw(mem, DS, FONT_SEG)
        font = bytes(mem.data[(font_seg << 4) & 0xFFFFF:(font_seg << 4) + 0x10000])
        font_base = _rw(mem, DS, FONT_BASE)
        pen = _rw(mem, DS, PEN)
        advance = mem.data[((DS << 4) + ADVANCE) & 0xFFFFF]
        page_draw = _rw(mem, DS, PAGE_DRAW)
        page_clear = _rw(mem, DS, PAGE_CLEAR)
        before = _read_planes(mem)

        # --- run the real ASM draw_string to its near-RET ---
        entry_sp = s.sp
        ret_ip = mem.rw(s.ss, entry_sp)
        ret_sp = (entry_sp + 2) & 0xFFFF
        steps = 0
        while not (s.cs == CS and s.ip == ret_ip and s.sp == ret_sp):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 2_000_000:
                results["incomplete"] += 1
                return
        after = _read_planes(mem)

        results["calls"] += 1
        # --- run the recovered draw_string on the before-image, diff planes 2|3 ---
        rec = [bytearray(p) for p in before]
        draw_string(rec, raw, font, font_base, pen, advance, page_draw, page_clear)
        ok = all(rec[p] == after[p] for p in (2, 3))
        results["verified" if ok else "diverged"] += 1

        if len(samples) < 4:
            changed = {p: [i for i in range(EGA_PLANE_STRIDE) if before[p][i] != after[p][i]]
                       for p in range(4)}
            foot = {p: (len(changed[p]), changed[p][:1] and hex(changed[p][0]))
                    for p in range(4) if changed[p]}
            samples.append(dict(ptr=text_ptr, text=raw.split(b"\x00")[0][:24],
                                font_seg=hex(font_seg), font_base=hex(font_base), pen=pen,
                                advance=advance, pg=(hex(page_draw), hex(page_clear)),
                                asm_writes=foot, recovered_ok=ok))

    cpu.replacement_hooks[(CS, DRAW_STRING)] = on_draw_string
    cpu.hook_names[(CS, DRAW_STRING)] = "probe:draw_string"

    held = False
    for f in range(700):
        try:
            rt.dos.pic.raise_irq(0)
            for _ in range(4000):
                cpu.step()
            if f > 30:                                  # press/release Enter to advance screens
                want = (f % 90) < 40
                if want and not held:
                    deliver_scancode(rt, 0x1C, max_steps=100000); held = True
                elif not want and held:
                    deliver_scancode(rt, 0x9C, max_steps=100000); held = False
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {f}: {type(exc).__name__}: {exc}")
            break
        if results["calls"] >= 8:
            break

    print(f"draw_string calls={results['calls']} verified={results['verified']} "
          f"diverged={results['diverged']} incomplete={results['incomplete']}")
    for i, s in enumerate(samples):
        print(f"  [{i}] {s}")
    if results["calls"] == 0:
        print("  (no draw_string call reached on cold boot — needs input to reach a text screen,"
              " or a user snapshot captured DURING a title/menu/score draw)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
