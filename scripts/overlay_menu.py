"""The F10 in-game overlay menu for the native product (visual style: pre2_editor's runtime menu).

Host-presentation layer ONLY — the determinism firewall is structural:
  * the menu is MODAL: while open the caller freezes the game tick (like the P pause) and routes every
    key event here, so nothing it consumes can ever reach the game's input cells or perturb a demo;
  * items act through caller-supplied closures on HOST/presentation settings (scaling, overlays, audio
    sink events) — this module imports nothing from pre2 and never touches a NativeGameState;
  * the Develop tab (cheats: they DO write game state, as a deliberate user action) exists only when the
    caller passes ``debug=True`` (the --debug flag) — hidden from the end-user product by default.

Items are data (the pre2_editor pattern): ``{"label": str, "value": str, "activate": fn, "adjust": fn(d)}``
per tab, supplied by a provider so values re-render live. Keys: F10/M/ESC close, Up/Down select,
Left/Right adjust (or switch tab when the item has no ``adjust``), Tab/PgUp/PgDn switch tab,
Enter/Space activate.

Settings persistence lives with the caller (a JSON next to the game data) — this module just edits the
dict through the closures.
"""
from __future__ import annotations

from typing import Any, Callable

Item = dict[str, Any]
TabsProvider = Callable[[], "list[tuple[str, list[Item]]]"]

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

    # --- fonts (lazy: pygame.font needs init) ----------------------------------------------------------
    def _fonts(self):
        if self._font is None:
            pg = self.pg
            self._font = pg.font.Font(None, 22)
            self._font_bold = pg.font.Font(None, 22)
            self._font_bold.set_bold(True)
            self._font_small = pg.font.Font(None, 17)
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
            self.item = (self.item - 1) % max(1, len(items))
        elif event.key in (pg.K_DOWN, pg.K_s):
            self.item = (self.item + 1) % max(1, len(items))
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

    def draw(self, screen) -> None:
        pg = self.pg
        font, bold, small = self._fonts()
        tabs = self._tabs()
        names = [t[0] for t in tabs]
        self.tab %= max(1, len(names))
        items = tabs[self.tab][1] if tabs else []
        if items:
            self.item %= len(items)

        win_w, win_h = screen.get_size()
        panel_w = min(max(360, win_w - 80), 640)
        panel_h = min(max(240, win_h - 80), 400)
        x = (win_w - panel_w) // 2
        y = (win_h - panel_h) // 2
        panel = pg.Surface((panel_w, panel_h), pg.SRCALPHA)
        panel.fill(_PANEL_BG)
        screen.blit(panel, (x, y))
        pg.draw.rect(screen, _PANEL_BORDER, (x, y, panel_w, panel_h), width=1)
        screen.blit(bold.render("PREHISTORIK 2", True, _TEXT_SELECTED), (x + 16, y + 12))

        # tabs
        tab_x, tab_y = x + 14, y + 40
        for i, name in enumerate(names):
            active = i == self.tab
            surf = (bold if active else font).render(f" {name} ", True,
                                                     (20, 20, 20) if active else _TEXT)
            rect = surf.get_rect(topleft=(tab_x, tab_y))
            rect.inflate_ip(8, 6)
            pg.draw.rect(screen, _TAB_ACTIVE_BG if active else _TAB_BG, rect)
            pg.draw.rect(screen, _TAB_BORDER, rect, width=1)
            screen.blit(surf, (tab_x + 4, tab_y + 3))
            tab_x += rect.width + 6

        # rows
        row_y, row_h = y + 78, 26
        for i, item in enumerate(items):
            selected = i == self.item
            if selected:
                pg.draw.rect(screen, _ROW_SELECTED, (x + 16, row_y - 3, panel_w - 32, row_h))
            screen.blit((bold if selected else font).render(str(item.get("label", "")), True,
                                                            _TEXT_SELECTED if selected else _TEXT),
                        (x + 26, row_y))
            val = font.render(str(item.get("value", "")), True,
                              _VALUE_SELECTED if selected else _VALUE)
            screen.blit(val, (x + panel_w - 28 - val.get_width(), row_y))
            row_y += row_h

        screen.blit(small.render("Up/Down select   Left/Right adjust or switch tab   Enter activate",
                                 True, _HELP), (x + 16, y + panel_h - 40))
        screen.blit(small.render("F10 / Esc close", True, _HELP), (x + 16, y + panel_h - 21))
