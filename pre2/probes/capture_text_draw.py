"""Witness + lockstep probe for the text leaf ``draw_string`` (``1030:9886``).

The scene island's first leaf needs a *mid-draw* witness: every snapshot is captured *after*
the text was drawn (font segment + VGA state gone), and ``draw_string`` fires only on menu /
score / tally **redraws**, not on cold boot or steady gameplay. The reliable way to reach it is
to **replay a demo that navigates the menus** — its real input drives the menu, where the mode-
select items are drawn.

This replays ``demo_pre2_20260622_192206`` (near-cold-start to level 1), hooks ``draw_string``,
and for each call captures its inputs + the VGA planes before, runs the real ASM to its RET,
captures the planes after, then runs the recovered :func:`pre2.recovered.text.draw_string` on
the before-image and diffs planes 2|3 vs the ASM — the byte-exact lockstep.

Result (2026-06-23): 24/24 menu draws byte-exact, 0 divergence ("MODE", "BEGINNER", ...).

Run:  python -m pre2.probes.capture_text_draw
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.recovered.text import draw_string
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
DRAW_STRING = 0x9886
DS = 0x1A0F                      # DGROUP — the text state block lives here
DEMO = ROOT / "artifacts" / "demo_pre2_20260622_192206"
# inferred state-block offsets (DGROUP), from pre2/recovered/text.py — all CONFIRMED below
FONT_SEG = 0x2875              # word: font glyph segment
FONT_BASE = 0xB1AC            # word: per-shade glyph base (offset into the font segment)
PEN = 0xB1A6                  # word: starting byte X
ADVANCE = 0xB1AB              # byte: per-char width
PAGE_DRAW = 0xB1A1            # word: draw page offset
PAGE_CLEAR = 0xB1A3           # word: clear page offset


def _rw(mem, seg, off):
    b = ((seg << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _read_planes(mem):
    return [bytearray(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE:
                               EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]) for p in range(4)]


def main() -> int:
    pb = InputDemoPlayback.load(DEMO)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", pb.snapshot_path(),
                            game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)))
    cpu = rt.cpu
    cpu.trace_enabled = False

    res = {"calls": 0, "verified": 0, "diverged": 0, "incomplete": 0}
    samples = []

    def on_draw_string(c):
        mem, s = c.mem, c.s
        raw = bytes(mem.data[((s.ds << 4) + s.bx) & 0xFFFFF:((s.ds << 4) + s.bx + 48) & 0xFFFFF])
        font_seg = _rw(mem, DS, FONT_SEG)
        font = bytes(mem.data[(font_seg << 4) & 0xFFFFF:(font_seg << 4) + 0x10000])
        font_base = _rw(mem, DS, FONT_BASE)
        pen = _rw(mem, DS, PEN)
        advance = mem.data[((DS << 4) + ADVANCE) & 0xFFFFF]
        page_draw = _rw(mem, DS, PAGE_DRAW)
        page_clear = _rw(mem, DS, PAGE_CLEAR)
        before = _read_planes(mem)

        entry_sp = s.sp
        ret_ip = mem.rw(s.ss, entry_sp)
        ret_sp = (entry_sp + 2) & 0xFFFF
        steps = 0
        while not (s.cs == CS and s.ip == ret_ip and s.sp == ret_sp):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 2_000_000:
                res["incomplete"] += 1
                return
        after = _read_planes(mem)

        res["calls"] += 1
        rec = [bytearray(p) for p in before]
        draw_string(rec, raw, font, font_base, pen, advance, page_draw, page_clear)
        ok = all(rec[p] == after[p] for p in (2, 3))
        res["verified" if ok else "diverged"] += 1
        if len(samples) < 6:
            samples.append(dict(text=raw.split(b"\x00")[0][:20], font_base=hex(font_base),
                                pen=pen, advance=advance, pg=(hex(page_draw), hex(page_clear)),
                                ok=ok))

    cpu.replacement_hooks[(CS, DRAW_STRING)] = on_draw_string
    cpu.hook_names[(CS, DRAW_STRING)] = "probe:draw_string"

    for f in range(int(meta.get("frames", 2000)) or 2000):
        try:
            pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {f}: {type(exc).__name__}: {exc}")
            break
        if res["calls"] >= 24:
            break

    print(f"draw_string calls={res['calls']} verified={res['verified']} "
          f"diverged={res['diverged']} incomplete={res['incomplete']}")
    for i, s in enumerate(samples):
        print(f"  [{i}] {s}")
    verdict = "PASS" if res["calls"] and not res["diverged"] and not res["incomplete"] else "CHECK"
    print(f"DRAW_STRING LOCKSTEP: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
