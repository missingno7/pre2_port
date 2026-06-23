"""Grounding check for the HUD status-bar layer (score / lives / energy).

Reads HudState from the bridge on the gameplay snapshots and asserts it matches the values
the HUD renders (read off the rendered frame): the 0x6C0E score *10 (displayed), [0x27D8]
lives, [0x27D6] energy hearts. Snapshot 185902 was eyeballed = score 5300, lives 2, energy 3.
"""
import sys; sys.path.insert(0, '.')
from pre2.runtime import load_pre2_snapshot
from pre2.bridge.render_state import read_renderer_state

# (snapshot, expected displayed score, lives, energy) — score read off the rendered HUD digits
EXPECT = {
    'gameplay_20260621_185902': (5300, 2, 3),
    'gameplay_20260622_010021': (32050, 2, 3),
    'gameplay_20260621_212037': (5600, 2, 3),
    'gameplay_20260622_003317': (31450, 2, 3),
}


def main():
    bad = []
    for nm, (score, lives, energy) in EXPECT.items():
        rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_' + nm,
                                game_root='assets', native_replacements=True)
        h = read_renderer_state(rt.cpu.mem).hud_state
        got = (h.score, h.lives, h.energy)
        ok = got == (score, lives, energy)
        print(f"  {nm[:22]}: score={h.score} lives={h.lives} energy={h.energy}  {'OK' if ok else f'!= {(score,lives,energy)}'}")
        if not ok:
            bad.append((nm, got, (score, lives, energy)))
    print("HUD STATE GROUNDING:", "PASS" if not bad else "FAIL")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
