"""Bridge: compose the level-end TALLY scene = black background + object overlay + the text panel.

The tally screen runs the same 0x2C controller loop as game-over (object pass + page flip, SCENE kind, no
6772), but with a BLACK background instead of a diorama image, plus the recovered text panel (SCORE /
LEVEL COMPLETED %). Black background is trivially a RecoveredBackground (all-zero); the object overlay is
the already-grounded object pass (reused from gameover_scene); the panel is render_tally_panel.
"""
from __future__ import annotations

from pre2.bridge.gameover_scene import _object_overlay
from pre2.bridge.render_state import read_renderer_state, retarget_page
from pre2.bridge.tally_panel import read_tally_panel
from pre2.recovered.scene_compositor import RecoveredBackground, compose_scene
from pre2.recovered.tally_panel import render_tally_panel

_BLACK = RecoveredBackground(tuple(bytes(0x10000) for _ in range(4)))


def _panel_overlay(inp):
    def overlay(planes, page):
        render_tally_panel(planes, inp.score, inp.percent, page,
                           inp.digit_font, inp.letters, inp.pct_glyph)
    return overlay


def build_tally_scene(mem, dos, *, game_root, page, panel_inputs=None):
    """Compose the tally scene: black background + object overlay + the recovered text panel.

    The count-up % increments between the 51A3 panel draw and the page flip, so the live viewer passes
    ``panel_inputs`` captured AT 51A3 (matching the drawn frame); otherwise they are read here.
    """
    rs = retarget_page(read_renderer_state(mem, dos, game_root=game_root), page)
    inp = panel_inputs if panel_inputs is not None else read_tally_panel(mem)
    return compose_scene(_BLACK, [_object_overlay(rs), _panel_overlay(inp)], page)
