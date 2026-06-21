"""TEMPORARY probe — in-VM lockstep verify of the recovered tracker/sequencer (227C).

Cold-boots with the emulated SB so the original music driver runs; at each
``1030:227C`` call (one sequencer tick, from the block-refill ISR) it captures the
playback state + the 4 voices + the current pattern / order table / period table /
instruments, runs the recovered ``tracker_tick`` on copies, lets the ASM run to its
RET, then diffs the resulting playback state and per-voice state. Zero divergence.

Retire when: a headless 227C lockstep is folded into the test suite.
Run:  python -m pre2.probes.verify_tracker
"""
from __future__ import annotations

import sys
from dataclasses import astuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.bridge import audio as A
from pre2.recovered.tracker import NUM_VOICES, tracker_tick
from pre2.runtime import create_pre2_runtime

TICK = (0x1030, 0x227C)
LIMIT = 300


def main() -> int:
    rt = create_pre2_runtime(str(ROOT / "assets" / "pre2.exe"),
                             game_root=str(ROOT / "assets"), fast_adlib=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    enable_sound_blaster(rt)
    pic = rt.dos.pic
    cpu.pending_irq = lambda: pic.acknowledge()

    state = {"ticks": 0, "rows": 0}
    diverged: list[str] = []

    def _run_to_return(c):
        # Detect the return by the pushed return address (the routine may clobber sp).
        entry_cs = c.s.cs & 0xFFFF
        ret_ip = c.mem.rw(c.s.ss & 0xFFFF, c.s.sp & 0xFFFF)
        fn = c.replacement_hooks.pop(TICK, None)
        nm = c.hook_names.pop(TICK, None)
        try:
            for _ in range(2_000_000):
                c.step()
                if (c.s.cs & 0xFFFF) == entry_cs and (c.s.ip & 0xFFFF) == ret_ip:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[TICK] = fn
            if nm is not None:
                c.hook_names[TICK] = nm

    def handler(c):
        mem = c.mem
        pb = A.read_playback(mem)
        voices = [A.read_voice(mem, ch) for ch in range(NUM_VOICES)]
        pattern = A.read_current_pattern(mem, pb.order_pos)
        order = A.read_order_table(mem)
        song_len = A.read_song_length(mem)
        period_table = A.read_period_table(mem)
        instruments = A.read_tracker_instruments(mem)
        will_process = pb.tick == 1  # this tick reloads speed and processes a row

        try:
            tracker_tick(pb, voices, pattern, order, song_len, period_table, instruments)
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to_return(c)
            cpu.replacement_hooks.pop(TICK, None)
            return

        _run_to_return(c)

        reason = None
        apb = A.read_playback(mem)
        if astuple(apb) != astuple(pb):
            reason = f"pb asm={astuple(apb)} rec={astuple(pb)}"
        if reason is None:
            for ch in range(NUM_VOICES):
                av = A.read_voice(mem, ch)
                if astuple(av) != astuple(voices[ch]):
                    reason = f"voice{ch} asm={astuple(av)} rec={astuple(voices[ch])}"
                    break

        state["ticks"] += 1
        if will_process:
            state["rows"] += 1
        if reason is not None:
            diverged.append(f"tick#{state['ticks']} ord={pb.order_pos} row={pb.row}: {reason}")
        if state["ticks"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(TICK, None)
            cpu.hook_names.pop(TICK, None)

    cpu.replacement_hooks[TICK] = handler
    cpu.hook_names[TICK] = "tracker_tick_verify"

    held = [False]
    for f in range(1400):
        pic.raise_irq(0)
        try:
            for _ in range(4000):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if TICK not in cpu.replacement_hooks:
            break
        if f > 40:
            want = (f % 120) < 50
            if want and not held[0]:
                deliver_scancode(rt, 0x1C, max_steps=100000); held[0] = True
            elif not want and held[0]:
                deliver_scancode(rt, 0x9C, max_steps=100000); held[0] = False

    print(f"tracker_tick verified: ticks={state['ticks']} rows-processed={state['rows']}")
    print(f"divergences={diverged[:6]}")
    ok = not diverged and state["rows"] > 0
    print("TRACKER LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
