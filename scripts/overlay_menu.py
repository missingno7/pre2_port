"""The F10 in-game overlay menu for the native product (visual style: pre2_editor's runtime menu).

Host-presentation layer ONLY — the determinism firewall is structural:
  * the menu is MODAL: while open the caller freezes the game tick (like the P pause) and routes every
    key event here, so nothing it consumes can ever reach the game's input cells or perturb a demo;
  * items act through caller-supplied closures on HOST/presentation settings (scaling, overlays, audio
    sink events) — this module imports nothing from pre2 and never touches a NativeGameState;
  * the Develop tab (cheats: they DO write game state, as a deliberate user action) exists only when the
    caller passes ``debug=True`` (the --debug flag) — hidden from the end-user product by default.

Items are data (the pre2_editor pattern): ``{"label": str, "value": str, "activate": fn, "adjust": fn(d)}``
per tab, supplied by a provider so values re-render live. An item with ``"info": True`` is a non-interactive
text row (small dim font, skipped by selection) — used for disclaimers. Keys: F10/M/ESC close, Up/Down select,
Left/Right adjust (or switch tab when the item has no ``adjust``), Tab/PgUp/PgDn switch tab,
Enter/Space activate.

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
_TEXT = (225, 225, 225)
_TEXT_SELECTED = (255, 255, 255)
_VALUE = (175, 195, 215)
_VALUE_SELECTED = (190, 235, 255)
_HELP = (210, 210, 210)
_HINT = (190, 210, 190)


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

    # --- fonts (lazy: pygame.font needs init; re-created when the UI scale changes, e.g. DPI / window size) --
    def _fonts(self, scale=1.0):
        key = max(1, int(round(scale * 4)))          # bucket the scale so we rebuild fonts only on real changes
        if self._font is None or self._font_key != key:
            pg = self.pg
            self._font_key = key
            self._font = pg.font.Font(None, max(10, int(round(22 * scale))))
            self._font_bold = pg.font.Font(None, max(10, int(round(22 * scale))))
            self._font_bold.set_bold(True)
            self._font_small = pg.font.Font(None, max(8, int(round(17 * scale))))
        return self._font, self._font_bold, self._font_small

    # --- input ------------------------------------------------------------------------------------------
    def handle_keydown(self, event) -> bool:
        """Consume one KEYDOWN while open. Returns False when the menu closed on this key."""
        pg = self.pg
        tabs = self._tabs()
        names = [t[0] for t in tabs]
        items = tabs[self.tab % len(tabs)][1] if tabs else []
        if event.key in (pg.K_F10, pg.K_m, pg.K_ESCAPE):
            self.open = False
            return False
        if event.key in (pg.K_UP, pg.K_w):
            self.item = _step_selectable(items, self.item, -1)
        elif event.key in (pg.K_DOWN, pg.K_s):
            self.item = _step_selectable(items, self.item, 1)
        elif event.key in (pg.K_PAGEUP, pg.K_q):
            self.tab = (self.tab - 1) % len(names)
            self.item = 0
        elif event.key in (pg.K_PAGEDOWN, pg.K_e, pg.K_TAB):
            self.tab = (self.tab + 1) % len(names)
            self.item = 0
        elif event.key in (pg.K_LEFT, pg.K_RIGHT):
            direction = -1 if event.key == pg.K_LEFT else 1
            item = items[self.item % len(items)] if items else {}
            adjust = item.get("adjust")
            if adjust is not None:
                adjust(direction)
            else:
                self.tab = (self.tab + direction) % len(names)
                self.item = 0
        elif event.key in (pg.K_RETURN, pg.K_SPACE):
            item = items[self.item % len(items)] if items else {}
            action = item.get("activate")
            if action is not None:
                action()
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
        panel_w = min(max(S(360), win_w - S(80)), S(640))
        row_h = S(26)
        n_rows = len(items) if items else 1
        panel_h = min(max(S(240), S(76) + n_rows * row_h + S(44)), win_h - S(40))
        x = (win_w - panel_w) // 2
        y = (win_h - panel_h) // 2
        panel = pg.Surface((panel_w, panel_h), pg.SRCALPHA)
        panel.fill(_PANEL_BG)
        screen.blit(panel, (x, y))
        pg.draw.rect(screen, _PANEL_BORDER, (x, y, panel_w, panel_h), width=max(1, S(1)))
        screen.blit(bold.render("Settings", True, _TEXT_SELECTED), (x + S(16), y + S(13)))

        # tabs — text centred (both axes) inside each chip
        tab_x, tab_y = x + S(14), y + S(40)
        for i, name in enumerate(names):
            active = i == self.tab
            surf = (bold if active else font).render(name, True, (20, 20, 20) if active else _TEXT)
            chip = pg.Rect(tab_x, tab_y, surf.get_width() + S(20), surf.get_height() + S(8))
            pg.draw.rect(screen, _TAB_ACTIVE_BG if active else _TAB_BG, chip)
            pg.draw.rect(screen, _TAB_BORDER, chip, width=max(1, S(1)))
            screen.blit(surf, surf.get_rect(center=chip.center))
            tab_x += chip.width + S(6)

        # rows — label/value vertically centred in the row band (matches the selection bar);
        # "info" rows are non-interactive text (small dim font, no value, never selected)
        if items and items[self.item].get("info"):
            self.item = _step_selectable(items, self.item, 1)
        row_y = y + S(76)
        for i, item in enumerate(items):
            row = pg.Rect(x + S(16), row_y, panel_w - S(32), row_h)
            if item.get("info"):
                text = small.render(str(item.get("label", "")), True, _HELP)
                screen.blit(text, text.get_rect(midleft=(x + S(26), row.centery)))
                row_y += row_h
                continue
            selected = i == self.item
            if selected:
                pg.draw.rect(screen, _ROW_SELECTED, row)
            label = (bold if selected else font).render(str(item.get("label", "")), True,
                                                        _TEXT_SELECTED if selected else _TEXT)
            screen.blit(label, label.get_rect(midleft=(x + S(26), row.centery)))
            val = font.render(str(item.get("value", "")), True,
                              _VALUE_SELECTED if selected else _VALUE)
            screen.blit(val, val.get_rect(midright=(x + panel_w - S(28), row.centery)))
            row_y += row_h

        screen.blit(small.render("Up/Down select   Left/Right adjust or switch tab   Enter activate",
                                 True, _HELP), (x + S(16), y + panel_h - S(40)))
        screen.blit(small.render("F10 / Esc close", True, _HELP), (x + S(16), y + panel_h - S(21)))
