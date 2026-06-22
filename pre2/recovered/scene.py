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
        -> render_scene(state, target)  (faithful leaves: image, text, cursor, palette)
        -> RenderTarget                 (faithful: VGA planes/linear + DAC | enhanced: own buffer)

PRE2 uses two video modes for scenes, so the seam handles both:
  * **mode 0Dh** — planar 16-colour (menu / map / score / tally): four EGA bitplanes; text via
    ``draw_string`` (planes 2|3).
  * **mode 13h** — linear 256-colour (intro / title artwork): a 320x200 indexed image.

Faithful ``render_scene`` reproduces the original pixels/palette for verification against the
VM/VGA oracle. A future native renderer supplies its own ``RenderTarget`` (e.g. a true-colour
buffer) and reimplements the leaves against the *same* ``SceneState`` — free of planar VRAM /
linear VGA / CRTC / page flips / the 6-bit DAC.

Leaf status (see ``docs/pre2/scene_island.md``):
  * text     = :func:`pre2.recovered.text.draw_string` (``1030:9886``) — RECOVERED (disasm-complete).
  * palette  = :func:`pre2.recovered.transition.fade_palette` (``6772``) — RECOVERED.
  * image    = :func:`present_image` — provisional (the exact ASM present is not yet recovered;
    the linear-13h copy is straightforward, the planar-0Dh present needs confirming).
  * cursor   = :func:`draw_cursor` — TBD (needs a menu witness).

Pure: no ``cpu``/``mem``/``dos_re`` imports. VM<->memory translation lives in
``pre2/bridge/scene_state.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pre2.recovered.render_frame import FadeStep
from pre2.recovered.text import draw_string
from pre2.recovered.transition import fade_palette

__all__ = [
    "RenderTarget", "SceneImage", "TextRun", "MenuHighlight", "SceneState", "FadeStep",
    "present_image", "draw_cursor", "render_scene",
    "MODE_PLANAR", "MODE_LINEAR",
    "SCENE_UNKNOWN", "SCENE_INTRO", "SCENE_TITLE", "SCENE_MENU",
    "SCENE_MAP", "SCENE_LOADING", "SCENE_TALLY",
]

MODE_PLANAR = 0x0D     # EGA 320x200x16 planar (menu / map / score / tally)
MODE_LINEAR = 0x13     # VGA 320x200x256 linear (intro / title artwork)

# Scene phase ids — context for the enhanced renderer + diagnostics (the scene state machine,
# once recovered, owns the transitions between these).
SCENE_UNKNOWN = 0
SCENE_INTRO = 1        # oldies / "Titus presents" / studio screens
SCENE_TITLE = 2
SCENE_MENU = 3         # main menu / mode select
SCENE_MAP = 4          # world map / level select
SCENE_LOADING = 5
SCENE_TALLY = 6        # level-end score / "bravo"


@dataclass
class RenderTarget:
    """Where :func:`render_scene` draws. The FAITHFUL target mirrors VGA memory:

    * mode 0Dh: ``planes`` = four EGA bitplane buffers;
    * mode 13h: ``linear`` = one 320x200 256-colour buffer.

    ``dac`` is the palette (a list of ``[r, g, b]``). The ENHANCED/native renderer supplies its
    own ``RenderTarget`` (e.g. a true-colour framebuffer) and reimplements the leaves against
    the same ``SceneState`` — nothing in ``SceneState`` assumes VGA."""
    planes: list | None = None
    linear: bytearray | None = None
    dac: list | None = None


@dataclass(frozen=True)
class SceneImage:
    """A full-screen background, in whichever encoding the scene's video mode uses:

    * ``pixels`` — linear 320x200 256-colour indices (mode 13h, intro/title);
    * ``planes`` — four EGA bitplanes (mode 0Dh, menu/map).

    The decoded picture asset (the SQZ codec is recovered; the *present* path is not yet). The
    enhanced renderer may instead carry a true-colour image — ``SceneState`` describes intent."""
    pixels: bytes | None = None
    planes: tuple | None = None       # (bytes, bytes, bytes, bytes)
    width: int = 320
    height: int = 200


@dataclass(frozen=True)
class TextRun:
    """One string drawn by ``draw_string`` (``1030:9886``): the bytes + the per-run pen/shade/
    page. ``font`` glyph bytes are shared at the :class:`SceneState` level. (Planar 0Dh only.)"""
    text: bytes
    font_base: int             # [0xB1AC] per-shade glyph base into the font segment
    pen: int                   # [0xB1A6] starting byte X (the pen advances per char)
    advance: int               # [0xB1AB] per-char width in bytes
    page_draw: int             # [0xB1A1] destination page offset
    page_clear: int            # [0xB1A3] clear page offset


@dataclass(frozen=True)
class MenuHighlight:
    """The selected menu item (the cursor) — the SEMANTIC selection.

    RECOVERED mechanism (menu witness via demo replay): the original has no separate cursor
    sprite — it **re-draws the selected item's text in a different shade** (``font_base``
    ``0x4200`` vs the normal ``0x0``). So the faithful path expresses the highlight by giving
    the selected item's :class:`TextRun` the highlight ``font_base`` (no extra draw); this field
    just carries the intent so the enhanced renderer can highlight however it likes (box, glow,
    …) from ``selection``."""
    selection: int
    item_count: int = 0
    highlight_font_base: int = 0x4200    # the shade the original re-draws the selected item in


@dataclass(frozen=True)
class SceneState:
    """Stable, VM-independent description of one non-gameplay screen (a small display list).

    Plain data only (no ``mem``); reconstructed by ``pre2.bridge.scene_state``."""
    scene_id: int = SCENE_UNKNOWN
    phase: str = ""
    video_mode: int = MODE_PLANAR
    background: SceneImage | None = None
    font: bytes = b""
    text_runs: tuple = ()                 # tuple[TextRun, ...]  (planar 0Dh only)
    palette: tuple | None = None          # N x [r, g, b] 6-bit static palette (16 or 256), or None
    fade: FadeStep | None = None          # a palette-fade step (overrides `palette` when set)
    cursor: MenuHighlight | None = None
    # Faithful page-flip bookkeeping (the enhanced renderer ignores these — it owns its buffer).
    page_visible: int = 0
    page_draw: int = 0


# --- leaves -----------------------------------------------------------------------

def present_image(target: RenderTarget, image: SceneImage, page: int = 0) -> None:
    """Lay a full-screen background into the target.

    Provisional faithful leaf. Linear (mode 13h) is a straight copy of the 256-colour image;
    planar (mode 0Dh) copies the four bitplanes at ``page``. The exact original present routine
    (whether it copies a page or draws direct, masking) is not yet recovered — this is the
    contract the recovered leaf will replace."""
    if image.pixels is not None and target.linear is not None:           # mode 13h
        n = min(len(image.pixels), len(target.linear))
        target.linear[:n] = image.pixels[:n]
    elif image.planes is not None and target.planes is not None:         # mode 0Dh
        for p in range(4):
            src = image.planes[p]
            target.planes[p][page:page + len(src)] = src


def draw_cursor(target: RenderTarget, cursor: MenuHighlight) -> None:
    """Draw the menu highlight.

    The original needs no separate draw here — it highlights by re-drawing the selected item's
    text in :attr:`MenuHighlight.highlight_font_base` (a shade swap), which the faithful path
    already does as a :class:`TextRun`. This hook is reserved for the ENHANCED renderer to draw
    its own highlight (box/glow/animation) from ``cursor.selection``; a no-op for the faithful
    path. The call site stays so the enhanced renderer has a place to plug in."""
    return None


def _apply_palette(state: SceneState, dac) -> None:
    """Resolve the scene's palette into ``dac`` (a fade step overrides a static palette)."""
    if dac is None:
        return
    if state.fade is not None:
        out, _done = fade_palette(state.fade.a, state.fade.b, state.fade.amount)
        for i in range(16):
            dac[i] = [out[3 * i] & 0x3F, out[3 * i + 1] & 0x3F, out[3 * i + 2] & 0x3F]
    elif state.palette is not None:
        for i in range(min(len(dac), len(state.palette))):
            dac[i] = list(state.palette[i])


def render_scene(state: SceneState, target: RenderTarget):
    """Render one non-gameplay scene into ``target``.

    Composes the leaves in z-order. Dispatches on ``state.video_mode``: a linear 256-colour
    scene (mode 13h) is a full-screen image + palette; a planar 16-colour scene (mode 0Dh) is a
    background + text runs + cursor + palette/fade. Writes ``target`` in place.

    The enhanced/native renderer is a separate reimplementation against the same ``SceneState``;
    this is the faithful, verification-oriented composition.
    """
    s = state

    # 1) background image (provisional leaf) — linear or planar per the scene's mode
    if s.background is not None:
        present_image(target, s.background, s.page_draw)

    # 2) text runs — draw_string (1030:9886), RECOVERED. Planar 0Dh only (it writes planes 2|3).
    if s.video_mode == MODE_PLANAR and target.planes is not None:
        for run in s.text_runs:
            draw_string(target.planes, run.text, s.font, run.font_base, run.pen, run.advance,
                        run.page_draw, run.page_clear)
        # 3) menu cursor / highlight (TBD leaf)
        if s.cursor is not None:
            draw_cursor(target, s.cursor)

    # 4) palette / fade -> DAC (fade_palette 6772, RECOVERED)
    _apply_palette(s, target.dac)
