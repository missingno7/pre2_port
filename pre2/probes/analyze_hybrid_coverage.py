"""Analyse the hybrid runtime over a recorded demo: how much of the executed instruction stream is PURE ASM
(interpreted) vs taken over by recovered-island REPLACEMENT hooks, and which hot ASM regions are unrecovered
(candidates for lifting). Uses cpu.coverage_telemetry (record_interpreted_instruction / record_hook_unverified).
Run: python pre2/probes/analyze_hybrid_coverage.py [demo_dir]
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from play import _advance_frame_deterministic, _make_replay_runtime

# Known CS:IP regions in segment 1030 (page = ip & 0xFF00), from the symbol ledger + project memory.
# label, role-class. role: 'spin' (idle busy-wait), 'render' (recovered offline, runs as ASM live),
# 'codec'/'audio'/'logic' game work.
_KNOWN = {
    0x9900: ("retrace busy-wait (9900)", "spin"),
    0x990D: ("retrace busy-wait (990D)", "spin"),
    0x44CD: ("retrace busy-wait (44CD)", "spin"),
    0x1C6F: ("PIT governor spin (1C6F)", "spin"),
    0x6772: ("main-loop frame boundary (6772)", "logic"),
    0x26FA: ("sprite render 26FA (object_render: RECOVERED offline)", "render"),
    0x65A0: ("object system 65A0 (draw primitives)", "render"),
    0x8BFF: ("object system 8BFF (hot-IP draw)", "render"),
    0x5C40: ("object dispatch 5C40", "render"),
    0x3668: ("animated-grid redraw 3668 (RECOVERED offline)", "render"),
    0x35A1: ("grid redraw 35A1 (RECOVERED offline)", "render"),
    0x3A27: ("scroll_copy 3A27 (RECOVERED offline)", "render"),
    0x348D: ("tile-row draw 348D (RECOVERED offline)", "render"),
    0x3B88: ("blit_sprite 3B88 (RECOVERED offline)", "render"),
    0x4B8E: ("particles 4B8E (RECOVERED offline)", "render"),
    0x3922: ("scroll script 3922", "logic"),
    0x3721: ("trigger 3721", "logic"),
}


class Coverage:
    def __init__(self):
        self.interp_page = Counter()    # (cs, ip&0xFF00) -> interpreted-instruction count
        self.hooks = Counter()          # hook name -> fires
        self.total_interp = 0
        self.total_hooks = 0

    def record_interpreted_instruction(self, key):
        self.total_interp += 1
        self.interp_page[(key[0], key[1] & 0xFF00)] += 1

    def record_hook_unverified(self, key, name):
        self.total_hooks += 1
        self.hooks[name] += 1


def main():
    demo = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260626_001513"
    playback = InputDemoPlayback.load(Path(demo))
    args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
                              timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250,
                              present_hz=120, retrace_pulse=0.06, verify=False)
    rt = _make_replay_runtime(args, playback)
    cov = Coverage()
    rt.cpu.coverage_telemetry = cov

    installed = dict(rt.cpu.hook_names)   # the replacement hooks active in the live game runtime

    det_speed = max(1, int(args.chunk_steps) * max(1, int(args.present_hz)))
    det_now = lambda: rt.cpu.instruction_count / det_speed   # noqa: E731
    rt.dos.time_source = det_now
    tick_state = {"next": 0.0}
    frame = 0
    while not playback.finished(frame):
        playback.apply_to_runtime(frame, rt,
                                  deliver=lambda runtime, sc: deliver_scancode(runtime, sc,
                                                                               max_steps=args.input_irq_steps))
        try:
            _advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000,
                                         clock=det_now, pic=rt.dos.pic, sound_blaster=None,
                                         timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                         tick_state=tick_state, det_speed=det_speed)
        except Exception as e:
            print(f"stopped at frame {frame}: {type(e).__name__}: {e}")
            break
        frame += 1

    total = cov.total_interp + cov.total_hooks
    print(f"demo: {demo}   frames: {frame}")
    print(f"installed replacement hooks: {len(installed)}")
    print(f"\n=== DISPATCH SPLIT (per executed step) ===")
    print(f"  pure-ASM interpreted instructions: {cov.total_interp:>12,}  ({100*cov.total_interp/total:.2f}%)")
    print(f"  recovered REPLACEMENT-hook fires:  {cov.total_hooks:>12,}  ({100*cov.total_hooks/total:.2f}%)")
    print(f"  (a hook fire replaces a whole ASM routine, so its share of WORK >> its share of steps)")

    def classify(cs, page):
        if cs != 0x1030:
            return (f"seg {cs:04X}", "other-seg")
        if page in (0x1C00,):
            return ("PIT governor spin (1C6F) - idle busy-wait", "spin")
        if page in (0x9900, 0x990D & 0xFF00, 0x44CD & 0xFF00):
            return ("VGA retrace busy-wait - idle", "spin")
        if 0x8000 <= page <= 0x8C00:
            return ("object system (65A0/8BFF draw + entity model)", "object-system")
        if 0x5C00 <= page <= 0x5F00:
            return ("object dispatch (5C40)", "object-system")
        if page in (0x6700,):
            return ("main-loop / frame governor (6772)", "logic")
        if 0x6000 <= page <= 0x6A00:
            return ("game/entity logic (6xxx)", "logic")
        if 0x3600 <= page <= 0x3B00:
            return ("renderer region (3xxx: tile/grid/scroll/blit - largely hooked)", "render")
        if 0x4900 <= page <= 0x4C00:
            return ("particles / 4Axx logic", "logic")
        if 0x7D00 <= page <= 0x7E00:
            return ("object handlers (7DA5/7DA9)", "logic")
        return ("(unrecovered)", "unrecovered")

    by_role = Counter()
    labelled = []
    for (cs, page), n in cov.interp_page.items():
        lbl, role = classify(cs, page)
        by_role[role] += n
        labelled.append((n, cs, page, lbl, role))
    print(f"\n=== INTERPRETED ASM by role ({cov.total_interp:,} instr) ===")
    for role, n in by_role.most_common():
        print(f"  {role:14s} {n:>12,}  ({100*n/cov.total_interp:.1f}%)")

    print(f"\n=== TOP 20 HOT INTERPRETED REGIONS (CS:IP page) ===")
    print(f"  {'count':>11} {'%':>6}  CS:page    label")
    for n, cs, page, lbl, role in sorted(labelled, reverse=True)[:20]:
        tag = lbl if lbl else ("(unrecovered)" if role == "unrecovered" else f"seg {cs:04X}")
        print(f"  {n:>11,} {100*n/cov.total_interp:>5.1f}%  {cs:04X}:{page:04X}  [{role}] {tag}")

    print(f"\n=== REPLACEMENT-HOOK FIRES (recovered islands active live) ===")
    for name, n in cov.hooks.most_common(20):
        print(f"  {n:>10,}  {name}")
    if not cov.hooks:
        print("  (none - game ran fully interpreted apart from the demo-clock waits)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
