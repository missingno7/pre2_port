"""Prehistorik 2 non-gameplay scene renderer — the ``SceneState -> render_scene`` seam.

The counterpart of :func:`pre2.recovered.render_frame.render_frame` for the **discrete**
screens (intro / title / oldies / menu / map / loading / tally). Where ``render_frame`` draws
a continuous scrolling tile world, ``render_scene`` draws a SCENE composed of a few primitives:
a full-screen background image, text runs, a menu cursor, and a palette (possibly fading).

``SceneState`` is the stable, VM-independent input contract — a plain-data DESCRIPTION of what
is on screen this frame (NOT how VGA draws it). Scene LOGIC (which scene, menu navigation,
transition timing) is the BORDER, exactly like the object system is for gameplay: it PRODUCES
``SceneState``; ``render_scene`` only DRAWS it.

    scene logic / state machine        (border)
        -> SceneState                   (this contract)
        -> render_scene(state, planes, dac)   (faithful leaves: image, text, cursor, palette)
        -> planar VRAM + 16-colour DAC  (faithful)   |   own buffer (enhanced/native)

Faithful ``render_scene`` reproduces the original pixels/palette for verification against the
VM/VGA oracle. A future native renderer reimplements it against the *same* ``SceneState``,
free of planar VRAM / CRTC / page flips / the 16-colour DAC.

Leaf status (see ``docs/pre2/scene_island.md``):
  * text     = :func:`pre2.recovered.text.draw_string` (``1030:9886``) — RECOVERED.
  * palette  = :func:`pre2.recovered.transition.fade_palette` (``6772``) — RECOVERED.
  * image    = :func:`present_image` — PROVISIONAL (exact ASM present not yet recovered).
  * cursor   = :func:`draw_cursor` — TBD (needs a menu witness).

Pure: no ``cpu``/``mem``/``dos_re`` imports. The VM<->memory translation lives in
``pre2/bridge/scene_state.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pre2.recovered.render_frame import FadeStep
from pre2.recovered.text import draw_string
from pre2.recovered.transition import fade_palette

__all__ = [
    "SceneImage", "TextRun", "MenuHighlight", "SceneState", "FadeStep",
    "present_image", "draw_cursor", "render_scene",
    "SCENE_UNKNOWN", "SCENE_INTRO", "SCENE_TITLE", "SCENE_MENU",
    "SCENE_MAP", "SCENE_LOADING", "SCENE_TALLY",
]

# Scene phase ids — context for the enhanced renderer + diagnostics (the scene state machine,
# once recovered, owns the transitions between these).
SCENE_UNKNOWN = 0
SCENE_INTRO = 1      # oldies / "Titus presents" / studio screens
SCENE_TITLE = 2
SCENE_MENU = 3       # main menu / mode select
SCENE_MAP = 4        # world map / level select
SCENE_LOADING = 5
SCENE_TALLY = 6      # level-end score / "bravo"

_PLANE_BYTES = 0x2000          # one EGA page is 0x2000 bytes/plane (320x200 / 8)


@dataclass(frozen=True)
class SceneImage:
    """A full-screen background as four EGA bitplanes (one byte = 8 px, ``_PLANE_BYTES`` each).

    The decoded picture asset (the SQZ codec is recovered; the *present* path is not yet).
    The enhanced renderer may instead carry a true-colour image — ``SceneState`` describes the
    intent, not the planar encoding."""
    planes: tuple              # (bytes, bytes, bytes, bytes)
    width: int = 320
    height: int = 200


@dataclass(frozen=True)
class TextRun:
    """One string drawn by ``draw_string`` (``1030:9886``): the bytes + the per-run pen/shade/
    page. ``font`` glyph bytes are shared at the :class:`SceneState` level."""
    text: bytes
    font_base: int             # [0xB1AC] per-shade glyph base into the font segment
    pen: int                   # [0xB1A6] starting byte X (the pen advances per char)
    advance: int               # [0xB1AB] per-char width in bytes
    page_draw: int             # [0xB1A1] destination page offset
    page_clear: int            # [0xB1A3] clear page offset


@dataclass(frozen=True)
class MenuHighlight:
    """The selected menu item (cursor). PROVISIONAL: how the original highlights it (palette
    cycle vs. a drawn marker vs. inverted text) is not yet recovered — needs a menu witness."""
    selection: int
    item_count: int = 0


@dataclass(frozen=True)
class SceneState:
    """Stable, VM-independent description of one non-gameplay screen (a small display list).

    Plain data only (no ``mem``); reconstructed by ``pre2.bridge.scene_state``."""
    scene_id: int = SCENE_UNKNOWN
    phase: str = ""
    background: SceneImage | None = None
    font: bytes = b""
    text_runs: tuple = ()                 # tuple[TextRun, ...]
    palette: tuple | None = None          # 16x [r, g, b] 6-bit static palette, or None
    fade: FadeStep | None = None          # a palette-fade step (overrides `palette` when set)
    cursor: MenuHighlight | None = None
    # Faithful page-flip bookkeeping (the enhanced renderer ignores these — it owns its buffer).
    page_visible: int = 0
    page_draw: int = 0


# --- leaves -----------------------------------------------------------------------

def present_image(planes, image: SceneImage, page: int = 0) -> None:
    """Lay a full-screen background into the four EGA planes at ``page``.

    PROVISIONAL faithful leaf: a straight planar copy of the picture's four bitplanes. The
    exact original present routine (video mode, masking, whether it copies a page or draws
    direct) is **not yet recovered** — this is the contract the recovered leaf will replace."""
    for p in range(4):
        src = image.planes[p]
        planes[p][page:page + len(src)] = src


def draw_cursor(planes, cursor: MenuHighlight) -> None:
    """Draw the menu highlight. TBD: no recovered routine yet (the menu state machine + its
    highlight mechanism need a witness). A no-op until recovered, so the seam is complete and
    the call site is already in place to merge into."""
    return None


def render_scene(state: SceneState, planes, dac=None):
    """Render one non-gameplay scene into ``planes`` (and ``dac``).

    ``planes`` is the four EGA plane buffers (faithful VGA target); ``dac``, if given, is the
    16-entry list of ``[r, g, b]`` 6-bit colours. Composes the leaves in z-order: background ->
    text -> cursor, with the palette/fade applied to the DAC. Returns nothing (writes in place).

    The enhanced/native renderer is a separate reimplementation against the same ``SceneState``;
    this is the faithful, verification-oriented composition.
    """
    s = state

    # 1) background image (provisional leaf)
    if s.background is not None:
        present_image(planes, s.background, s.page_draw)

    # 2) text runs — draw_string (1030:9886), RECOVERED
    for run in s.text_runs:
        draw_string(planes, run.text, s.font, run.font_base, run.pen, run.advance,
                    run.page_draw, run.page_clear)

    # 3) menu cursor / highlight (TBD leaf)
    if s.cursor is not None:
        draw_cursor(planes, s.cursor)

    # 4) palette / fade -> DAC (fade_palette 6772, RECOVERED). Fade overrides a static palette.
    if dac is not None:
        if s.fade is not None:
            out, _done = fade_palette(s.fade.a, s.fade.b, s.fade.amount)
            for i in range(16):
                dac[i] = [out[3 * i] & 0x3F, out[3 * i + 1] & 0x3F, out[3 * i + 2] & 0x3F]
        elif s.palette is not None:
            for i in range(min(16, len(s.palette))):
                dac[i] = list(s.palette[i])
