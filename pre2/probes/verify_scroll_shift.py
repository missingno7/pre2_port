"""Throwaway witness: lockstep the recovered menu/scene framebuffer scroll (1030:9804..9876).

The mode-select's hottest code is the 4-plane A000 self-copy that shifts the displayed buffer
to follow the camera (Part 1: 8-px horizontal boundary column shift; Part 2: vertical shift by
the scroll_y delta). This hooks the block, runs the recovered scroll_shift_frame on the
before-planes, drives the real ASM to the exit (9877), and diffs the four EGA planes. Snapshot
075918 bounces, so stepping it exercises both parts many times.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from pre2.bridge.object_render import read_planes
from pre2.recovered.present import scroll_shift_frame
from pre2.runtime import load_pre2_snapshot

ROOT = Path(__file__).resolve().parents[2]
CS, ENTRY, EXIT = 0x1030, 0x9804, 0x9877
_DS = 0x1A0F


def _rw(mem, off):
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def main(snapshot="artifacts/snapshot_pre2_modeselect_20260623_075918", max_frames=40):
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", ROOT / snapshot,
                            game_root=ROOT / "assets", native_replacements=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    s = cpu.s
    res = {"frames": 0, "ok": 0, "bad": 0, "part1": 0, "part2": 0}

    def probe(c):
        mem = c.mem
        b199 = mem.data[(_DS << 4) + 0xB199]
        sx, sy, psy, pd = _rw(mem, 0xB19D), _rw(mem, 0xB19F), _rw(mem, 0xB19B), _rw(mem, 0xB1A1)
        bp = s.bp
        rec = [bytearray(p) for p in read_planes(mem)]
        scroll_shift_frame(rec, b199, sx, sy, psy, pd, wrap=bp)
        steps = 0
        while not (s.cs == CS and s.ip == EXIT):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 400_000:
                res["bad"] += 1
                return
        after = read_planes(mem)
        ok = all(rec[p] == after[p] for p in range(4))
        res["frames"] += 1
        res["ok" if ok else "bad"] += 1
        if (b199 & 8) != (sx & 8):
            res["part1"] += 1
        if ((sy - psy) & 0xFFFF) != 0:
            res["part2"] += 1

    cpu.replacement_hooks[(CS, ENTRY)] = probe
    cpu.hook_names[(CS, ENTRY)] = "probe:scroll_shift"
    for _ in range(2_000_000):
        try:
            cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped: {type(exc).__name__}: {exc}")
            break
        if res["frames"] >= max_frames:
            break
    print(f"SCROLL_SHIFT: frames={res['frames']} ok={res['ok']} bad={res['bad']} "
          f"(part1={res['part1']} part2={res['part2']})")
    return res


if __name__ == "__main__":
    main()
