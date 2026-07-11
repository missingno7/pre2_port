"""Unit tests for the Android front-end menu logic — the NEW GAME / CONTINUE button hit-testing, the CONTINUE
level-select grid unlock/hit rules, and the progress save. Pure geometry (pygame.Rect) + a temp-file save; no
display, no game data.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SIZE = (1280, 720)


# -- NEW GAME / CONTINUE buttons ---------------------------------------------------------------------
def test_menu_button_hit_test():
    from android_menu import menu_button_at, menu_button_rects
    rects = menu_button_rects(SIZE)
    assert menu_button_at(rects["new"].center, SIZE) == "new"
    assert menu_button_at(rects["continue"].center, SIZE) == "continue"
    assert menu_button_at((5, 5), SIZE) is None                 # top-left corner hits neither button
    assert menu_button_at(None, SIZE) is None                   # no tap
    # the two buttons never overlap
    assert not rects["new"].colliderect(rects["continue"])


# -- CONTINUE grid -----------------------------------------------------------------------------------
def test_continue_columns_and_branch():
    from android_menu import BEGINNER_LEVELS, EXPERT_LEVELS, ContinueScreen
    cs = ContinueScreen({"beginner": 99, "expert": 99})         # everything unlocked
    cells, _back, *_ = cs._grid(SIZE)
    beg = [c for c in cells if not c["expert"]]
    exp = [c for c in cells if c["expert"]]
    assert len(beg) == BEGINNER_LEVELS
    assert len(exp) == EXPERT_LEVELS
    # the expert tail is the branch: expert has fewer password checkpoints, so the columns differ in length
    assert BEGINNER_LEVELS != EXPERT_LEVELS


def test_continue_unlock_by_progress():
    from android_menu import ContinueScreen
    # reached beginner level index 2 (L3) and no expert progress at all
    cs = ContinueScreen({"beginner": 2, "expert": -1})
    # a reached beginner cell selects its (level, expert)
    hit = cs.hit(_cell_center(cs, 2, expert=False), SIZE)
    assert hit == (2, False)
    # levels 0..2 unlocked, level 3 still locked -> a tap on it is ignored
    assert cs.hit(_cell_center(cs, 0, expert=False), SIZE) == (0, False)
    assert cs.hit(_cell_center(cs, 3, expert=False), SIZE) is None
    # expert path untouched -> even L1 expert is locked
    assert cs.hit(_cell_center(cs, 0, expert=True), SIZE) is None


def test_continue_back_button():
    from android_menu import ContinueScreen
    cs = ContinueScreen({"beginner": 0, "expert": -1})
    _cells, back, *_ = cs._grid(SIZE)
    assert cs.hit(back.center, SIZE) == "back"


def _cell_center(cs, level, *, expert):
    cells, *_ = cs._grid(SIZE)
    for c in cells:
        if c["level"] == level and c["expert"] == expert:
            return c["rect"].center
    raise AssertionError(f"no cell for level={level} expert={expert}")


# -- Back-button pause dialog --------------------------------------------------------------------------
def test_pause_dialog_hits_and_menu_gating():
    from android_menu import PauseDialog
    dlg = PauseDialog(include_menu=True)
    rects = dlg.rects(SIZE)
    assert set(rects) == {"resume", "menu", "exit"}
    assert dlg.hit(rects["resume"].center, SIZE) == "resume"
    assert dlg.hit(rects["menu"].center, SIZE) == "menu"
    assert dlg.hit(rects["exit"].center, SIZE) == "exit"
    assert dlg.hit((5, 5), SIZE) is None                     # outside taps keep the dialog open (no accidental resume)
    assert dlg.hit(None, SIZE) is None
    # buttons never overlap
    rl = list(rects.values())
    assert not any(a.colliderect(b) for i, a in enumerate(rl) for b in rl[i + 1:])
    # in the front-end the Main-menu option is dropped (menu-to-menu is meaningless)
    fe = PauseDialog(include_menu=False)
    assert set(fe.rects(SIZE)) == {"resume", "exit"}
    assert fe.hit(fe.rects(SIZE)["exit"].center, SIZE) == "exit"


# -- progress save -----------------------------------------------------------------------------------
def test_progress_record_and_persist(tmp_path):
    import android_host
    root = str(tmp_path)
    prog = android_host.load_progress(root)
    assert prog == {"beginner": -1, "expert": -1}               # first run: nothing reached
    assert android_host.record_reached(prog, 0, False, root)    # reached beginner L1 -> advances
    assert android_host.record_reached(prog, 3, False, root)    # reached beginner L4 -> advances
    assert not android_host.record_reached(prog, 1, False, root)  # a lower level does NOT regress the furthest
    assert android_host.record_reached(prog, 2, True, root)     # expert path tracked independently
    # BONUS / special levels (id >= 0x0A) are reachable from a main level but must NOT advance the furthest
    # (else entering one bonus unlocks the whole column). It stays at the last real level.
    assert not android_host.record_reached(prog, 0x0B, False, root)
    assert prog["beginner"] == 3
    # persisted and reloads identically
    reloaded = android_host.load_progress(root)
    assert reloaded == {"beginner": 3, "expert": 2}
