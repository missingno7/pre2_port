"""In-game overlay settings menu — the NATIVE product's F10 menu (tabbed, mouse+keyboard, pygame-injected).

Promoted from the first completed port's `overlay_menu.py`. This is a POST-ENDGAME widget: it belongs to
the native product only — the hybrid/VM runtimes are recovery instruments and stay pristine (their F10 is
a screenshot key). See template_dos_port's `docs/post_endgame.md` for when and how to use it.

Host-presentation layer ONLY — the determinism firewall is structural:
  * the menu is MODAL: while open the caller freezes the game tick (like a pause) and routes every key
    event here, so nothing it consumes can ever reach the game's input cells or perturb a demo;
  * items act through caller-supplied closures on HOST/presentation settings — this module imports
    nothing from any game and never touches game state;
  * pygame is INJECTED (the ``pygame_mod`` constructor arg): importing this module needs nothing.

The tab convention the callers follow (the accuracy taxonomy — enforce it in your tabs_provider):
  * presentation tabs (Display / Audio / ...): READ-ONLY enhancements, parity-gated, faithful defaults;
  * an **Experimental** tab: anything that can affect game accuracy (state-writing opt-ins) is
    quarantined here — labeled, default OFF, never mixed in with the safe toggles;
  * a debug/cheats tab (they write game state as a deliberate user action) exists only when the caller
    passes ``debug=True``-style gating — hidden from the end-user product by default.

Items are data: ``{"label": str, "value": str, "activate": fn, "adjust": fn(d)}`` per tab, supplied by a
provider so values re-render live. An item with ``"info": True`` is a non-interactive text row (small dim
font, skipped by selection) — used for disclaimers. Keys: F10/M/ESC close, Up/Down select, Left/Right
adjust (or switch tab when the item has no ``adjust``), Tab/PgUp/PgDn switch tab, Enter/Space activate.

Settings persistence lives with the caller (a JSON next to the game data) — this module just edits the
dict through the closures.
"""
from __future__ import annotations

from typing import Any, Callable

Item = dict[str, Any]
TabsProvider = Callable[[], "list[tuple[str, list[Item]]]"]


def _step_selectable(items, current: int, direction: int) -> int:
    """Next selectable (non-info) item index in ``direction``, wrapping; stays put if none are selectable."""
    if not items:
        return 0
    for step in range(1, len(items) + 1):
        i = (current + direction * step) % len(items)
        if not items[i].get("info"):
            return i
    return current

_PANEL_BG = (12, 14, 18, 230)          # editor: translucent near-black panel
_PANEL_BORDER = (180, 180, 180)
_TAB_ACTIVE_BG = (210, 220, 235)
_TAB_BG = (48, 54, 66)
_TAB_BORDER = (100, 110, 130)
_ROW_SELECTED = (56, 84, 120)
_ROW_EDITING = (72, 118, 170)        # brighter bar while a row is "entered" for Left/Right editing
_TEXT = (225, 225, 225)
_TEXT_SELECTED = (255, 255, 255)
_VALUE = (175, 195, 215)
_VALUE_SELECTED = (190, 235, 255)
_HELP = (210, 210, 210)
_HINT = (190, 210, 190)
_MAX_VISIBLE_ROWS = 8            # cap the rows band; beyond this the list scrolls (slim scrollbar on the right)

# A clean, light UI face rather than pygame's default (freesansbold — heavy, and faux-bolding it looks broad &
# cramped). SysFont tries each name in order; the DejaVu/Arial tail covers Linux/mac when Segoe UI is absent.
_UI_FACES = "segoeui,segoe ui,selawik,helveticaneue,helvetica neue,dejavusans,dejavu sans,arial"


def _load_font(pg, size, bold):
    """A regular- (or real-bold-) weight UI font at ``size``, falling back to pygame's default if no system
    font is found (headless CI). Real bold from the face — never the synthetic set_bold broadening."""
    try:
        f = pg.font.SysFont(_UI_FACES, size, bold=bold)
        if f is not None:
            return f
    except Exception:                          # noqa: BLE001 — no fontconfig / headless
        pass
    f = pg.font.Font(None, size + 4)           # default font renders a touch smaller per point -> nudge up
    if bold:
        f.set_bold(True)
    return f


class OverlayMenu:
    """The tabbed overlay. ``tabs_provider()`` returns ``[(tab_name, [items...]), ...]`` fresh each frame."""

    def __init__(self, pygame_mod, tabs_provider: TabsProvider):
        self.pg = pygame_mod
        self._tabs = tabs_provider
        self.open = False
        self.tab = 0
        self.item = 0
        self._font = None
        self._font_bold = None
        self._font_small = None
        self._font_key = None
        self._scroll = 0                             # first visible row index (rows scroll; the panel is capped)
        self.editing = False                         # an adjustable row is "entered" -> Left/Right change it
        self._hover = -1                             # item index under the mouse (-1 = none); shows ‹ › affordance
        self._hit = {"panel": None, "tabs": [], "rows": [], "arrows": {}}   # layout rects from the last draw

    # --- fonts (lazy: pygame.font needs init; re-created when the UI scale changes, e.g. DPI / window size) --
    def _fonts(self, scale=1.0):
        key = max(1, int(round(scale * 4)))          # bucket the scale so we rebuild fonts only on real changes
        if self._font is None or self._font_key != key:
            pg = self.pg
            self._font_key = key                     # SysFont points render larger than Font(None) units -> 16/13
            self._font = _load_font(pg, max(9, int(round(16 * scale))), False)
            self._font_bold = _load_font(pg, max(9, int(round(16 * scale))), True)
            self._font_small = _load_font(pg, max(7, int(round(13 * scale))), False)
        return self._font, self._font_bold, self._font_small

    # --- input ------------------------------------------------------------------------------------------
    def handle_keydown(self, event) -> bool:
        """Consume one KEYDOWN while open. Returns False when the menu closed on this key.

        Interaction model (no Left/Right ambiguity): Left/Right ALWAYS switch tabs, EXCEPT while a row is
        "entered" for editing. Enter/Space on an adjustable row ENTERS it (then Left/Right change the value,
        Enter/Space CONFIRMS -> applies + exits, Esc CANCELS -> exits without applying). Enter/Space on a
        plain toggle/action row fires it immediately."""
        pg = self.pg
        tabs = self._tabs()
        names = [t[0] for t in tabs]
        items = tabs[self.tab % len(tabs)][1] if tabs else []
        cur = items[self.item % len(items)] if items else {}

        if event.key in (pg.K_F10, pg.K_m):          # F10/M always close outright
            self.open = self.editing = False
            return False
        if event.key == pg.K_ESCAPE:                 # Esc backs out: cancel an edit first, else close
            if self.editing:
                self.editing = False
                return True
            self.open = False
            return False

        if self.editing:                             # --- EDITING an adjustable row ---
            adjust = cur.get("adjust")
            if event.key in (pg.K_LEFT, pg.K_a) and adjust:
                adjust(-1)
            elif event.key in (pg.K_RIGHT, pg.K_d) and adjust:
                adjust(1)
            elif event.key in (pg.K_RETURN, pg.K_SPACE):     # confirm -> apply (level jump etc.) + exit
                self.editing = False
                act = cur.get("activate")
                if act is not None:
                    act()
            elif event.key in (pg.K_UP, pg.K_w):             # move away -> exit edit, then move
                self.editing = False
                self.item = _step_selectable(items, self.item, -1)
            elif event.key in (pg.K_DOWN, pg.K_s):
                self.editing = False
                self.item = _step_selectable(items, self.item, 1)
            return True

        # --- NOT editing ---
        if event.key in (pg.K_UP, pg.K_w):
            self.item = _step_selectable(items, self.item, -1)
        elif event.key in (pg.K_DOWN, pg.K_s):
            self.item = _step_selectable(items, self.item, 1)
        elif event.key in (pg.K_PAGEUP, pg.K_q, pg.K_LEFT):
            self.tab = (self.tab - 1) % len(names)
            self.item = 0
        elif event.key in (pg.K_PAGEDOWN, pg.K_e, pg.K_TAB, pg.K_RIGHT):
            self.tab = (self.tab + 1) % len(names)
            self.item = 0
        elif event.key in (pg.K_RETURN, pg.K_SPACE):
            if cur.get("adjust") is not None:        # adjustable -> enter edit mode (Left/Right now change it)
                self.editing = True
            else:                                    # plain toggle / action -> fire immediately
                action = cur.get("activate")
                if action is not None:
                    action()
        return True

    def handle_mouse(self, event) -> bool:
        """Consume one mouse event (motion / wheel / button) while open, using the hit rects recorded by the
        last ``draw``. Returns False when the menu closed. Click a tab to switch; hover to highlight; click a
        toggle to fire it; click an adjustable row (or its ‹ / › ) to change it; wheel to scroll; click outside
        the panel to close."""
        pg = self.pg
        hit = self._hit
        tabs = self._tabs()
        items = tabs[self.tab % len(tabs)][1] if tabs else []

        if event.type == pg.MOUSEMOTION:
            self._hover = -1
            for rect, i in hit.get("rows", []):
                if i < len(items) and rect.collidepoint(event.pos) and not items[i].get("info"):
                    self._hover = i
                    if i != self.item:               # hovering a new row selects it (and cancels any edit)
                        self.item, self.editing = i, False
                    break
            return True

        if event.type == pg.MOUSEWHEEL:              # scroll the list (if it scrolls)
            n, vis = hit.get("n", 0), hit.get("visible", 0)
            self._scroll = max(0, min(self._scroll - event.y, max(0, n - vis)))
            return True

        if event.type != pg.MOUSEBUTTONDOWN:
            return True
        if event.button not in (1, 3):               # only left / right clicks act
            return True
        pos = event.pos
        if hit.get("panel") is not None and not hit["panel"].collidepoint(pos):
            self.open = self.editing = False         # click outside the panel closes
            return False
        for rect, i in hit.get("tabs", []):          # a tab chip
            if rect.collidepoint(pos):
                self.tab, self.item, self.editing = i, 0, False
                return True
        for rect, i in hit.get("rows", []):          # a row
            if i >= len(items) or not rect.collidepoint(pos) or items[i].get("info"):
                continue
            self.item = i
            item = items[i]
            adjust, act = item.get("adjust"), item.get("activate")
            if adjust is not None:
                la, ra = hit.get("arrows", {}).get(i, (None, None))
                if event.button == 3 or (la is not None and la.collidepoint(pos)):
                    adjust(-1)                       # right-click or the ‹ arrow -> previous
                elif ra is not None and ra.collidepoint(pos):
                    adjust(1)                        # the › arrow -> next
                elif act is not None:
                    act()                            # body click on an apply-row -> apply
                else:
                    adjust(1)                        # body click on a plain cycle-row -> next
            elif act is not None and event.button == 1:
                act()                                # a toggle / action row fires
            return True
        return True

    # --- drawing (at WINDOW resolution, over the already-scaled game frame) ------------------------------
    def draw_hint(self, screen) -> None:
        """The discreet closed-state hint (editor style)."""
        _, _, small = self._fonts()
        screen.blit(small.render("F10 menu", True, _HINT), (8, 6))

    def draw(self, screen, scale=1.0) -> None:
        """Draw the overlay. ``scale`` (>= 1) is the UI scale — the caller passes the display's DPI / a
        resolution factor so the panel + text stay a readable PHYSICAL size on hi-DPI / 4K screens."""
        pg = self.pg
        s = max(1.0, float(scale))

        def S(v):
            return int(round(v * s))
        font, bold, small = self._fonts(s)
        tabs = self._tabs()
        names = [t[0] for t in tabs]
        self.tab %= max(1, len(names))
        items = tabs[self.tab][1] if tabs else []
        if items:
            self.item %= len(items)

        win_w, win_h = screen.get_size()
        panel_w = min(max(S(340), win_w - S(80)), S(560))
        row_h = S(26)
        footer = S(44)                               # footer help text height
        # --- lay the tab chips out with WRAPPING: chips flow left-to-right and drop to a new line when the row
        #     is full, so any number of tabs fits. Widths are measured with the BOLD font (the active-chip face)
        #     so the layout never reflows as the selection moves. Positions are panel-relative (x/y added below).
        tab_left, tab_top = S(14), S(40)
        tab_area_w = panel_w - 2 * S(14)
        chip_h = bold.get_height() + S(8)
        line_gap = S(6)
        tab_layout, cx, cy = [], tab_left, 0
        for i, name in enumerate(names):
            cw = bold.size(name)[0] + S(20)
            if cx > tab_left and cx + cw > tab_left + tab_area_w:     # doesn't fit -> wrap to the next line
                cx, cy = tab_left, cy + chip_h + line_gap
            tab_layout.append((cx, cy, cw, i))
            cx += cw + S(6)
        top = tab_top + (cy + chip_h) + S(12)        # rows band starts below the (possibly multi-line) tab block
        n_rows = len(items) if items else 1
        # FIXED panel height across all tabs: the rows band is always ``visible`` rows tall (capped at
        # _MAX_VISIBLE_ROWS, or fewer only if the window is short) — so a short tab (Audio/Experimental) is the
        # same size as View instead of a thin strip; extra rows scroll.
        fit_rows = max(1, (win_h - S(40) - top - footer) // row_h)
        visible = max(1, min(_MAX_VISIBLE_ROWS, fit_rows))
        panel_h = top + visible * row_h + footer
        x = (win_w - panel_w) // 2
        y = (win_h - panel_h) // 2
        self._hit = {"panel": pg.Rect(x, y, panel_w, panel_h), "tabs": [], "rows": [], "arrows": {}}
        panel = pg.Surface((panel_w, panel_h), pg.SRCALPHA)
        panel.fill(_PANEL_BG)
        screen.blit(panel, (x, y))
        pg.draw.rect(screen, _PANEL_BORDER, (x, y, panel_w, panel_h), width=max(1, S(1)))
        screen.blit(bold.render("Settings", True, _TEXT_SELECTED), (x + S(16), y + S(13)))

        # tabs — text centred (both axes) inside each chip, at the pre-wrapped positions
        for cx_rel, cy_rel, cw, i in tab_layout:
            active = i == self.tab
            surf = (bold if active else font).render(names[i], True, (20, 20, 20) if active else _TEXT)
            chip = pg.Rect(x + cx_rel, y + tab_top + cy_rel, cw, chip_h)
            pg.draw.rect(screen, _TAB_ACTIVE_BG if active else _TAB_BG, chip)
            pg.draw.rect(screen, _TAB_BORDER, chip, width=max(1, S(1)))
            screen.blit(surf, surf.get_rect(center=chip.center))
            self._hit["tabs"].append((pg.Rect(chip), i))

        # scroll so the selected row stays in the visible band
        if items and items[self.item].get("info"):
            self.item = _step_selectable(items, self.item, 1)
        if self.item < self._scroll:
            self._scroll = self.item
        elif self.item >= self._scroll + visible:
            self._scroll = self.item - visible + 1
        self._scroll = max(0, min(self._scroll, max(0, n_rows - visible)))
        self._hit["visible"], self._hit["n"] = visible, n_rows   # for mouse-wheel scroll clamping
        scrollbar = n_rows > visible
        sb_pad = S(14) if scrollbar else 0           # keep row text/values clear of the scrollbar

        # rows — label/value vertically centred in the row band (matches the selection bar);
        # "info" rows are non-interactive text (small dim font, no value, never selected)
        row_y = y + top
        for i in range(self._scroll, min(n_rows, self._scroll + visible)):
            item = items[i]
            row = pg.Rect(x + S(16), row_y, panel_w - S(32) - sb_pad, row_h)
            if item.get("info"):
                text = small.render(str(item.get("label", "")), True, _HELP)
                screen.blit(text, text.get_rect(midleft=(x + S(26), row.centery)))
                row_y += row_h
                continue
            self._hit["rows"].append((pg.Rect(row), i))
            selected = i == self.item
            adjustable = item.get("adjust") is not None
            # show the ‹ › affordance when the row is being edited (keyboard) OR hovered by the mouse
            arrows = adjustable and (selected and self.editing or self._hover == i)
            if selected:
                pg.draw.rect(screen, _ROW_EDITING if (self.editing and adjustable) else _ROW_SELECTED, row)
            label = (bold if selected else font).render(str(item.get("label", "")), True,
                                                        _TEXT_SELECTED if selected else _TEXT)
            screen.blit(label, label.get_rect(midleft=(x + S(26), row.centery)))
            vtext = f"‹ {item.get('value', '')} ›" if arrows else str(item.get("value", ""))
            val = font.render(vtext, True, _VALUE_SELECTED if selected else _VALUE)
            vrect = val.get_rect(midright=(x + panel_w - S(28) - sb_pad, row.centery))
            screen.blit(val, vrect)
            if arrows:                               # clickable ‹ (left third) / › (right third) of the value
                third = vrect.width // 3
                self._hit["arrows"][i] = (pg.Rect(vrect.left, row.top, third, row.height),
                                          pg.Rect(vrect.right - third, row.top, third, row.height))
            row_y += row_h

        if scrollbar:                                # a slim track + proportional thumb on the right
            track_x = x + panel_w - S(10)
            track_y, track_h = y + top, visible * row_h
            pg.draw.rect(screen, _TAB_BG, (track_x, track_y, S(4), track_h))
            thumb_h = max(S(16), int(track_h * visible / n_rows))
            thumb_y = track_y + int((track_h - thumb_h) * self._scroll / max(1, n_rows - visible))
            pg.draw.rect(screen, _VALUE, (track_x, thumb_y, S(4), thumb_h))

        hint = ("Left/Right change   Enter/Space apply   Esc cancel" if self.editing
                else "Up/Down select   Left/Right switch tab   Enter/Space change")
        screen.blit(small.render(hint, True, _HELP), (x + S(16), y + panel_h - S(40)))
        screen.blit(small.render("F10 / Esc close", True, _HELP), (x + S(16), y + panel_h - S(21)))
