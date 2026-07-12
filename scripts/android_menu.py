"""Android-specific front-end menus — the NEW GAME / CONTINUE buttons over the press-1/2 screen, and the
graphical CONTINUE level-select (a 2-column BEGINNER | EXPERT checkpoint grid that unlocks with progress).

This is the phone-native replacement for the original's keyboard menu: '1'/'2' aren't reachable on a
touchscreen, and the ENTER-CODE password screen is a poor fit for thumbs. NEW GAME just presses '1' (the
byte-exact beginner mode-select). CONTINUE lets the player pick any checkpoint they have already reached and
jumps straight to it — writing the SAME ``[0x2D8A]`` / ``[0xB197]`` a valid password would (see
``pre2.recovered.password``), so it can't affect gameplay accuracy; it's a friendlier front door to the
game's own checkpoint system.

All rendering + hit-testing lives here (pygame, like the rest of ``scripts/``); the pure game gesture math
stays in ``pre2.native.touch``. Kept out of ``play_native.py`` / ``android_host.py`` so the mobile menu UI is
one self-contained unit.

Level counts are per PLAYABLE path: 9 beginner (ends at the penguin level [0x2D8A]=0x08) + 10 expert (runs to
the mode-9 boss [0x2D8A]=0x09) — beginner is the shorter path, expert has the longer expert-only tail.
"""
from __future__ import annotations

import pygame

# The checkpoint grid = the game's actually-PLAYABLE levels per path (NOT the password count, which is a
# separate encoding). Selecting cell i commits [0x2D8A]=i, so the counts are set by how far each path runs:
#   * BEGINNER ends early — its last playable level is the penguin level [0x2D8A]=0x08 (the "you must be an
#     EXPERT eater to continue" wall), i.e. 9 levels (indices 0..8);
#   * EXPERT runs the full main progression to the mode-9 boss [0x2D8A]=0x09 (pre2.native.loop), i.e. 10
#     levels (indices 0..9) — the expert-only tail is why its column is longer.
BEGINNER_LEVELS = 9         # L1..L9   -> [0x2D8A] 0..8 (last = penguin 0x08)
EXPERT_LEVELS = 10          # L1..L10  -> [0x2D8A] 0..9 (last = mode-9 boss 0x09), [0xB197]=1

# translucent skin, tuned to read over the bright menu art (RGBA)
_BTN_FILL = (18, 24, 34, 205)
_BTN_EDGE = (150, 210, 255, 235)
_BTN_TEXT = (238, 244, 252, 255)
_PANEL_BG = (10, 12, 20, 255)          # the CONTINUE screen is a full opaque panel (its own screen, not an overlay)
_TITLE = (238, 244, 252, 255)
_COL_HEAD = (150, 210, 255, 255)
_CELL_OPEN = (32, 92, 148, 255)        # an unlocked (reached) checkpoint
_CELL_OPEN_EDGE = (150, 210, 255, 255)
_CELL_LOCK = (26, 30, 40, 255)         # a not-yet-reached checkpoint
_CELL_LOCK_EDGE = (60, 68, 84, 255)
_CELL_TEXT = (238, 244, 252, 255)
_CELL_LOCK_TEXT = (96, 104, 120, 255)
_BACK_TEXT = (200, 214, 232, 255)


def _font(px: int):
    return pygame.font.Font(None, max(10, int(px)))


# ---------------------------------------------------------------------------------------------------
# NEW GAME / CONTINUE buttons — drawn over the MENU.SQZ press-1/2 screen, tapped via the menu gestures.
# ---------------------------------------------------------------------------------------------------
def menu_button_rects(size) -> dict:
    """The two front-door buttons for the press-1/2 screen: NEW GAME then CONTINUE, centred low so they sit
    under the title art and fall to the thumbs. Window-pixel rects keyed by name ('new' / 'continue')."""
    w, h = size
    bw = max(120, int(w * 0.34))
    bh = max(40, int(h * 0.13))
    gap = int(bh * 0.30)
    cx = w // 2
    y0 = int(h * 0.52)
    return {
        "new": pygame.Rect(cx - bw // 2, y0, bw, bh),
        "continue": pygame.Rect(cx - bw // 2, y0 + bh + gap, bw, bh),
    }


def menu_button_at(pos, size) -> str | None:
    """Which front-door button (if any) contains ``pos`` — 'new', 'continue', or None (tap outside = ignored)."""
    if pos is None:
        return None
    for name, rect in menu_button_rects(size).items():
        if rect.collidepoint(pos):
            return name
    return None


class MenuButtons:
    """Draws the NEW GAME / CONTINUE buttons as cheap static sprites (built once per size, like the touch
    overlay), so compositing them every presented sub-frame is a couple of quads, not a window-size upload."""

    _LABELS = {"new": "NEW GAME", "continue": "CONTINUE"}

    def __init__(self) -> None:
        self._spr = None                                          # {"size", "new", "continue"} cached per window size

    def _pill(self, w, h):
        s = pygame.Surface((w, h), pygame.SRCALPHA)
        r = h // 2
        pygame.draw.rect(s, _BTN_FILL, s.get_rect(), border_radius=r)
        pygame.draw.rect(s, _BTN_EDGE, s.get_rect(), width=max(2, h // 22), border_radius=r)
        return s

    def _build(self, disp, size):
        rects = menu_button_rects(size)
        spr = {"size": size}
        for name, rect in rects.items():
            s = self._pill(rect.w, rect.h)
            font = _font(rect.h * 0.5)
            t = font.render(self._LABELS[name], True, _BTN_TEXT[:3])
            s.blit(t, t.get_rect(center=(rect.w // 2, rect.h // 2)))
            spr[name] = (disp.make_sprite(s), rect.topleft)
        self._spr = spr

    def draw(self, disp, size) -> None:
        if self._spr is None or self._spr["size"] != size:
            self._build(disp, size)
        for name in ("new", "continue"):
            sprite, topleft = self._spr[name]
            disp.draw_sprite(sprite, topleft)


# ---------------------------------------------------------------------------------------------------
# Back-button pause dialog — Resume / Main menu / Exit, over the dimmed frozen frame.
# ---------------------------------------------------------------------------------------------------
class PauseDialog:
    """The Android Back-button dialog. MODAL like the F10 menu: the caller freezes the game while it is
    open and routes every event here, so opening it can never perturb gameplay or a demo. ``include_menu``
    drops the 'Main menu' option when the front-end is already showing (menu-to-menu is meaningless).

    ``hit`` returns ``"resume"`` / ``"menu"`` / ``"exit"`` or ``None`` (a tap outside the buttons keeps the
    dialog open — an accidental palm must not resume into a running game)."""

    def __init__(self, *, include_menu: bool = True) -> None:
        self.include_menu = include_menu
        self._spr = None                                          # cached sprites per (size, include_menu)

    def _entries(self):
        entries = [("resume", "RESUME")]
        if self.include_menu:
            entries.append(("menu", "MAIN MENU"))
        entries.append(("exit", "EXIT GAME"))
        return entries

    def rects(self, size) -> dict:
        """Window-pixel button rects keyed by action, stacked and centred."""
        w, h = size
        entries = self._entries()
        bw = max(140, int(w * 0.30))
        bh = max(40, int(h * 0.12))
        gap = int(bh * 0.35)
        total = len(entries) * bh + (len(entries) - 1) * gap
        y0 = (h - total) // 2 + int(h * 0.03)                     # nudged down to clear the PAUSED title
        cx = w // 2
        return {action: pygame.Rect(cx - bw // 2, y0 + i * (bh + gap), bw, bh)
                for i, (action, _label) in enumerate(entries)}

    def hit(self, pos, size) -> str | None:
        if pos is None:
            return None
        for action, rect in self.rects(size).items():
            if rect.collidepoint(pos):
                return action
        return None

    def _build(self, disp, size):
        rects = self.rects(size)
        labels = dict(self._entries())
        spr = {"size": size, "include_menu": self.include_menu, "buttons": []}
        for action, rect in rects.items():
            s = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
            r = rect.h // 2
            pygame.draw.rect(s, _BTN_FILL, s.get_rect(), border_radius=r)
            pygame.draw.rect(s, _BTN_EDGE, s.get_rect(), width=max(2, rect.h // 22), border_radius=r)
            t = _font(rect.h * 0.45).render(labels[action], True, _BTN_TEXT[:3])
            s.blit(t, t.get_rect(center=(rect.w // 2, rect.h // 2)))
            spr["buttons"].append((disp.make_sprite(s), rect.topleft))
        title = _font(int(size[1] * 0.075)).render("PAUSED", True, _TITLE[:3])
        ts = pygame.Surface(title.get_size(), pygame.SRCALPHA)
        ts.blit(title, (0, 0))
        first_top = min(r.top for r in rects.values())
        spr["title"] = (disp.make_sprite(ts), ((size[0] - title.get_width()) // 2,
                                               max(8, first_top - int(size[1] * 0.13))))
        self._spr = spr

    def draw(self, disp) -> None:
        """Draw the title + buttons as cached sprites (the caller draws the frozen frame + dim first)."""
        size = disp.get_size()
        if self._spr is None or self._spr["size"] != size or self._spr["include_menu"] != self.include_menu:
            self._build(disp, size)
        sprite, topleft = self._spr["title"]
        disp.draw_sprite(sprite, topleft)
        for sprite, topleft in self._spr["buttons"]:
            disp.draw_sprite(sprite, topleft)


# ---------------------------------------------------------------------------------------------------
# CONTINUE level-select — a full-screen 2-column BEGINNER | EXPERT checkpoint grid.
# ---------------------------------------------------------------------------------------------------
class ContinueScreen:
    """The graphical CONTINUE level picker. Two columns of checkpoint cells (BEGINNER / EXPERT); a cell is
    unlocked once the player has reached that level on that path (``progress`` from ``android_host``). Tapping an
    unlocked cell returns ``(level, expert)`` — the ``[0x2D8A]`` / ``[0xB197]`` to commit. A BACK button (or a
    tap outside the grid) returns to the menu.

    The player having reached level N unlocks every checkpoint 0..N on that path (so you can always resume at or
    before where you last were). Beginner tops out at :data:`BEGINNER_LEVELS`; expert has the longer tail — the
    last levels are expert-only, which the shorter beginner column shows at a glance."""

    def __init__(self, progress: dict) -> None:
        self._beg = int(progress.get("beginner", -1))            # furthest 0-based level reached, per path
        self._exp = int(progress.get("expert", -1))
        self._surf = None                                         # cached render; rebuilt on resize
        self._surf_size = None

    # -- geometry -------------------------------------------------------------------------------------
    def _grid(self, size):
        """Layout the two columns. Returns ``(cells, back_rect)`` where each cell is a dict with its rect, the
        (level, expert) it selects, and whether it is unlocked."""
        w, h = size
        cols = [("BEGINNER", BEGINNER_LEVELS, False, self._beg),
                ("EXPERT", EXPERT_LEVELS, True, self._exp)]
        top = int(h * 0.20)                                       # below the title
        bottom = int(h * 0.90)                                    # above the BACK row
        rows = max(BEGINNER_LEVELS, EXPERT_LEVELS)
        cell_h = max(16, int((bottom - top) / rows * 0.82))
        pitch = (bottom - top) / rows
        col_w = min(int(w * 0.30), 360)
        col_gap = int(w * 0.10)
        block_w = col_w * 2 + col_gap
        x0 = (w - block_w) // 2
        cells = []
        for ci, (_head, n, expert, reached) in enumerate(cols):
            cx = x0 + ci * (col_w + col_gap)
            for i in range(n):
                y = int(top + i * pitch)
                rect = pygame.Rect(cx, y, col_w, cell_h)
                cells.append({"rect": rect, "level": i, "expert": expert, "unlocked": i <= reached,
                              "label": f"{'EXPERT' if expert else 'LEVEL'} {i + 1}"})
        back_rect = pygame.Rect(int(w * 0.5) - int(w * 0.15), int(h * 0.925), int(w * 0.30), int(h * 0.06))
        return cells, back_rect, x0, top, col_w, col_gap

    # -- hit test -------------------------------------------------------------------------------------
    def hit(self, pos, size):
        """Resolve a tap. Returns ``(level, expert)`` for an unlocked cell, the string ``"back"`` for the BACK
        button, or ``None`` (a locked cell or empty space — ignored)."""
        if pos is None:
            return None
        cells, back_rect, *_ = self._grid(size)
        if back_rect.collidepoint(pos):
            return "back"
        for c in cells:
            if c["rect"].collidepoint(pos):
                return (c["level"], c["expert"]) if c["unlocked"] else None
        return None

    # -- rendering ------------------------------------------------------------------------------------
    def _render(self, size):
        w, h = size
        surf = pygame.Surface(size)
        surf.fill(_PANEL_BG[:3])
        cells, back_rect, x0, top, col_w, col_gap = self._grid(size)
        # title
        tf = _font(int(h * 0.09))
        title = tf.render("CONTINUE", True, _TITLE[:3])
        surf.blit(title, title.get_rect(center=(w // 2, int(h * 0.10))))
        # column headers
        hf = _font(int(h * 0.045))
        for ci, head in enumerate(("BEGINNER", "EXPERT")):
            cx = x0 + ci * (col_w + col_gap) + col_w // 2
            t = hf.render(head, True, _COL_HEAD[:3])
            surf.blit(t, t.get_rect(center=(cx, int(top - h * 0.035))))
        # cells
        cf = _font(int(h * 0.032))
        for c in cells:
            r = c["rect"]
            fill = _CELL_OPEN if c["unlocked"] else _CELL_LOCK
            edge = _CELL_OPEN_EDGE if c["unlocked"] else _CELL_LOCK_EDGE
            txtcol = _CELL_TEXT if c["unlocked"] else _CELL_LOCK_TEXT
            pygame.draw.rect(surf, fill[:3], r, border_radius=max(3, r.h // 5))
            pygame.draw.rect(surf, edge[:3], r, width=max(1, r.h // 16), border_radius=max(3, r.h // 5))
            label = c["label"]                               # locked cells read as locked from the grey styling
            #   alone — the default pygame font has no padlock glyph (it rendered as a tofu box)
            t = cf.render(label, True, txtcol[:3])
            surf.blit(t, t.get_rect(center=r.center))
        # BACK
        pygame.draw.rect(surf, _CELL_LOCK[:3], back_rect, border_radius=back_rect.h // 2)
        pygame.draw.rect(surf, _CELL_LOCK_EDGE[:3], back_rect, width=2, border_radius=back_rect.h // 2)
        bf = _font(int(back_rect.h * 0.55))
        bt = bf.render("BACK", True, _BACK_TEXT[:3])
        surf.blit(bt, bt.get_rect(center=back_rect.center))
        return surf

    def draw(self, disp) -> None:
        """Render the CONTINUE screen full-window and present it. Cached until the window resizes (it's static
        apart from that), so redrawing each frame of the modal loop is a single texture draw, not a re-render."""
        size = disp.get_size()
        changed = self._surf is None or self._surf_size != size
        if changed:
            self._surf = self._render(size)
            self._surf_size = size
        disp.present_ui(self._surf, changed=changed)
