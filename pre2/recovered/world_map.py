"""The world-MAP / level-select screen (1030:8E45 dispatch + 1030:96D5 scroll controller).

After the title sequence (and the SPRITES.SQZ decode at 2dfa), the front-end runs the world-map level-select:
a 0Dh planar map that pans (the "bouncing camera") with the level markers drawn on top, entered from a 13h
"press 1/2" difficulty screen. It is driven by a state machine at ``8E45..8EFD`` that, per difficulty, calls the
``96D5`` scroll controller with a per-frame UI callback (``991F`` / ``9985``) that stamps the level text and
reads input; the chosen level goes to ``[0x2D8A]`` and the map hands off to the level loader (``447D``/``3ED6``).

This module recovers that screen leaf-by-leaf. The render leaves already exist (``recovered.menu_scene``'s
``MenuScenePage`` is really this map's page; ``recovered.carte`` is the carte column-blit) — what is recovered
here is the controller's per-frame logic.

Recovered so far:
  * :func:`map_pan` — the CRTC pan ([asm 97A8-97F7]).

Pure: no ``cpu``/``mem``/``dos_re`` imports.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link

_DISPLAY_WRAP = 0x1FFF       # [asm 97BB] and bh,0x1f  (the 0x2000-paragraph display-start wrap)
_ROW_STRIDE = 0x28           # [asm 97A8] 40 bytes per row
_SCROLL_STEP = 4             # [asm 9AE6] the map auto-scrolls left 4 fine-units per frame
_BLIT_UNIT = 0x1080          # [asm 9B1A] (prev_x & 7) * 0x1080 — the page self-copy source offset


@oracle_link("1030:97A8",
             "world-map CRTC pan: EGA display-start = (0x28*[0xB19F] + [0xB19D]>>3) & 0x1FFF, pel-pan = "
             "[0xB19D] & 7, from the map camera [0xB19D] (fine X word) / [0xB19F] (row word).",
             "VERIFIED", merge_target="render_scene")
def map_pan(camera_x: int, camera_row: int) -> tuple[int, int]:
    """[asm 97A8-97F7] The world-map's CRTC pan from its camera position.

    ``camera_x`` is the 16-bit ``[0xB19D]`` (the fine X scroll, a wrapping word — e.g. ``0xFFFC`` pans left
    of the origin); ``camera_row`` is the 16-bit ``[0xB19F]`` (the row). Returns ``(display_start, pel)``:
    the EGA CRTC display-start (start address, masked to the 0x2000-word panning window) and the attribute-
    controller pel-pan (0..7). ``mul`` keeps only the low 16 bits before the ``& 0x1FFF``, so the formula is
    exact for any 16-bit inputs."""
    display_start = (_ROW_STRIDE * (camera_row & 0xFFFF) + ((camera_x & 0xFFFF) >> 3)) & _DISPLAY_WRAP
    pel = camera_x & 7                                       # [asm 97F2-97F5] mov al,[0xB19D]; and al,7
    return display_start, pel


@dataclass(frozen=True)
class MapCamera:
    """The world-map camera state. ``x``/``row`` are :func:`map_pan`'s inputs; ``prev_x``/``prev_row`` are the
    previous frame's values the page self-copy uses; ``blit_off`` = ``(prev_x & 7) * 0x1080`` ([0xB1AC])."""
    x: int            # [0xB19D] fine X (a wrapping word)
    row: int          # [0xB19F] row
    phase: int        # [0xB1A5] bounce phase counter (byte)
    prev_x: int       # [0xB199]
    prev_row: int     # [0xB19B]
    blit_off: int     # [0xB1AC]


@oracle_link("1030:9AE0",
             "world-map camera update: scroll left 4/frame ([0xB19D]-=4); the row bounces by "
             "(signed sine_table[++phase] >> 4) when cs:[5] is set; derive [0xB199]/[0xB19B]/[0xB1AC].",
             "VERIFIED", merge_target="render_scene")
def map_camera_update(cam: MapCamera, *, bounce: bool, sine_table: bytes) -> MapCamera:
    """[asm 9AE0] Advance the bouncing map camera one frame.

    The camera scrolls left a fixed 4 fine-units (``[0xB19D] -= 4``); when ``bounce`` (``cs:[5] != 0``) the row
    is nudged by ``(signed)sine_table[phase] >> 4`` (arithmetic) with ``phase`` pre-incremented, and the old row
    is stashed in ``prev_row``. ``prev_x`` always takes the old X, and ``blit_off = (prev_x & 7) * 0x1080``. When
    ``bounce`` is off the row and ``prev_row`` are left unchanged (the ASM skips that block)."""
    new_x = (cam.x - _SCROLL_STEP) & 0xFFFF                  # [asm 9AE6] sub ax,4
    prev_x = cam.x & 0xFFFF                                  # [asm 9AE9-9AED] xchg -> [0xB199] = old X
    phase = (cam.phase + 1) & 0xFF                           # [asm 9AF0] inc [0xB1A5]
    new_row = cam.row & 0xFFFF
    prev_row = cam.prev_row & 0xFFFF
    if bounce:                                               # [asm 9AF5] cmp cs:[5],0; je
        b = sine_table[phase]                                # [asm 9B00-9B03] xlatb
        if b >= 0x80:
            b -= 0x100                                       # [asm 9B04] cbw (signed)
        new_row = (cam.row + (b >> 4)) & 0xFFFF              # [asm 9B05-9B0D] sar ax,4 ; add [0xB19F]
        prev_row = cam.row & 0xFFFF                          # [asm 9B11] xchg -> [0xB19B] = old row
    blit_off = ((prev_x & 7) * _BLIT_UNIT) & 0xFFFF          # [asm 9B14-9B1F]
    return MapCamera(new_x, new_row, phase, prev_x, prev_row, blit_off)


# --- the mode-select screen (1030:991F, the 96D5 callback for [0x2D8A]==0) -----------------------
# The "press difficulty" screen: a scrolling background (map_camera_update) with "MODE" + the chosen
# difficulty drawn on top; an arrow key toggles BEGINNER<->EXPERT, fire confirms.
_MODE_TEXT = 0xB180          # "MODE"      (DGROUP string)
_BEGINNER_TEXT = 0xB185      # "BEGINNER"
_EXPERT_TEXT = 0xB18E        # " EXPERT "
_ARROW_SCANCODES = (0x48, 0x4B, 0x4D, 0x50)   # up / left / right / down [asm 9965-9977]


@dataclass(frozen=True)
class MenuTextRun:
    """One ``draw_string`` run the menu callback issues: the DGROUP address of the NUL-terminated string plus
    the pen (``[0xB1A6]``) and per-char advance (``[0xB1AB]``) it sets. The font/page come from the controller."""
    addr: int
    pen: int
    advance: int


@oracle_link("1030:991F",
             "mode-select draw: 'MODE' (pen 0xC38, adv 4 @0xB180) then 'BEGINNER' (@0xB185) or ' EXPERT ' "
             "(@0xB18E, chosen by [0xB197]) (pen 0x1185, adv 3).",
             "VERIFIED", merge_target="render_scene")
def mode_select_text_runs(selection: int) -> tuple[MenuTextRun, MenuTextRun]:
    """[asm 991F-994B] The two text runs the mode-select screen draws: ``MODE`` then the chosen difficulty —
    ``BEGINNER`` when ``[0xB197] == 0`` else `` EXPERT ``."""
    difficulty = _EXPERT_TEXT if selection else _BEGINNER_TEXT   # [asm 9941-9948] cmp [0xB197],0; jne -> 0xB18E
    return (MenuTextRun(_MODE_TEXT, 0x0C38, 4), MenuTextRun(difficulty, 0x1185, 3))


@dataclass(frozen=True)
class ModeSelectInput:
    """The mode-select input step's effects on state: ``toggle`` flips ``[0xB197]`` (BEGINNER/EXPERT);
    ``confirm`` bumps ``[0xB1B2]`` (the chosen-difficulty exit counter); ``consume`` clears ``[0x2874]``."""
    toggle: bool
    confirm: bool
    consume: bool


@oracle_link("1030:994E",
             "mode-select input: arrow (0x48/4B/4D/50) toggles [0xB197]; fire ([0x282d]|[0x2810]) bumps "
             "[0xB1B2]; a consumed key clears [0x2874]. A released key (bit7) is a no-op.",
             "VERIFIED", merge_target="render_scene")
def mode_select_input(scancode: int, fire: bool) -> ModeSelectInput:
    """[asm 994E-9984] One mode-select input step.

    ``scancode`` is the latched key ``[0x2874]`` (bit7 set = a release event); ``fire`` is ``[0x282d] | [0x2810]``
    (the confirm flag). A release is a no-op; fire confirms (bumps ``[0xB1B2]``) without consuming; otherwise the
    key is consumed (``[0x2874]=0``) and an arrow toggles the BEGINNER/EXPERT selection (``[0xB197] ^= 1``)."""
    if scancode & 0x80:                                          # [asm 9952-9955] released -> no-op
        return ModeSelectInput(False, False, False)
    if fire:                                                     # [asm 9957-995E] [0x282d]|[0x2810] -> confirm
        return ModeSelectInput(False, True, False)
    toggle = (scancode & 0xFF) in _ARROW_SCANCODES              # [asm 9965-9977] an arrow
    return ModeSelectInput(toggle, False, True)                 # [asm 9960] consume the key


# --- the 96D5 controller's per-frame plumbing (the 97A8 loop, minus the callback) ----------------

@oracle_link("1030:97CA",
             "map controller page management: the draw page [0xB1A1] becomes the new CRTC display-start, the "
             "clear page [0xB1A3] becomes the previous draw page (the scrolling single-buffer page flip).",
             "VERIFIED", merge_target="render_scene")
def map_page_flip(display_start: int, prev_page_draw: int) -> tuple[int, int]:
    """[asm 97CA-97CE] ``xchg [0xB1A1], bx`` then ``[0xB1A3] = bx``: the new draw page is the display-start
    (from :func:`map_pan`), and the clear page becomes the previous draw page. Returns ``(page_draw, page_clear)``."""
    return display_start & 0xFFFF, prev_page_draw & 0xFFFF


@oracle_link("1030:9804",
             "map page self-copy gate: scroll_shift fires when the fine-X crosses an 8-unit (byte) boundary, "
             "i.e. (prev_x & 8) != (x & 8) (the `and ax,0x808; cmp al,ah` test).",
             "VERIFIED", merge_target="render_scene")
def should_scroll_shift(prev_x: int, x: int) -> bool:
    """[asm 9804-9810] Whether the map page self-copy (``scroll_shift_frame``) runs this frame: the controller
    pans the buffer by one byte-column only when the camera's fine-X has crossed an 8-unit boundary."""
    return (prev_x & 8) != (x & 8)


# --- the password-entry screen (1030:9985, the 96D5 callback for [0x2D8A]==0xFF) ------------------
# "ENTER CODE": a scrolling background with a 4-character level code typed on top; each hex key is
# parsed to a nibble and accumulated into the 16-bit code [0xB1B9], then validated (the 932F generator,
# [[pre2.recovered.password]]) once 4 chars are in.
_ENTER_CODE_TEXT = 0xB175    # "ENTER CODE"
_CODE_BUFFER = 0xB170        # the 4-char typed buffer ("[[[" when empty)
_PASSWORD_LEN = 4            # [asm 9A1F] cmp bl,4


@oracle_link("1030:9985",
             "password-entry draw: 'ENTER CODE' (pen 0xAF2, adv 3 @0xB175) then the 4-char typed code buffer "
             "(pen 0x12C9, adv 4 @0xB170); font_base 0x4200 highlights the active slot.",
             "VERIFIED", merge_target="render_scene")
def password_text_runs() -> tuple[MenuTextRun, MenuTextRun]:
    """[asm 9988-99A7] The two text runs the password screen draws: the ``ENTER CODE`` label then the typed
    code buffer (whose bytes evolve as the player types — the caller reads them from state)."""
    return (MenuTextRun(_ENTER_CODE_TEXT, 0x0AF2, 3), MenuTextRun(_CODE_BUFFER, 0x12C9, 4))


@oracle_link("1030:99E1",
             "password key -> hex nibble: the scancode's ASCII (table @0xB068) maps a hex key to a nibble "
             "(char-'0', minus 7 more if > 9); the sentinel '-' (0x2D) means a non-hex key (rejected).",
             "VERIFIED", merge_target="render_scene")
def password_hex_value(ascii_char: int) -> int | None:
    """[asm 99DD-99F2] Parse a typed (uppercase) ASCII character to a hex nibble 0..15, or ``None`` if it is
    not a hex digit (the ASCII table yields ``'-'`` (0x2D) for non-hex keys)."""
    if ascii_char == 0x2D:                       # [asm 99E1] cmp al,0x2d; je (invalid)
        return None
    nibble = (ascii_char - 0x30) & 0xFF          # [asm 99EA] sub ah,0x30
    if nibble > 9:                               # [asm 99ED] cmp ah,9; jbe
        nibble -= 7                              # [asm 99F2] sub ah,7  ('A'->10 .. 'F'->15)
    return nibble & 0x0F


@oracle_link("1030:99F5",
             "password code accumulate: code = (code << 4) | nibble (16-bit), the typed hex shifted into "
             "[0xB1B9] one nibble at a time.",
             "VERIFIED", merge_target="render_scene")
def password_accumulate(code: int, nibble: int) -> int:
    """[asm 99F5-9A0D] Shift a new hex nibble into the 16-bit entered code ``[0xB1B9]`` (``shl ah,4`` then four
    ``shl ah,1; rcl dx,1`` = ``(code << 4) | nibble``)."""
    return ((code << 4) | (nibble & 0x0F)) & 0xFFFF


# --- the level-select dispatcher (1030:8E45, main loop 8E7B-8E96) ---------------------------------
# The front-end's level-select state machine: after a 13h prompt it loops, entering the mode-select or
# password screen on a flag/keypress, or auto-advancing to the default level after a timeout.
LS_MODE_SELECT = "mode_select"     # [asm 8EC9] -> 96D5 callback 991F
LS_PASSWORD = "password"           # [asm 8EDE] -> 96D5 callback 9985
LS_WAIT = "wait"                   # [asm 8E96] keep counting [0x27F0]
LS_AUTO_ADVANCE = "auto_advance"   # [asm 8E98] timeout -> the default level from [0x83E]
_LS_TIMEOUT = 0x10E                # [asm 8E90] cmp [0x27F0], 0x10E


@oracle_link("1030:8E7B",
             "level-select dispatch: enter mode-select if [0x27F6] or input ([0x282d]|[0x2810]); password if "
             "[0x27F7]; else count [0x27F0] up to 0x10E and auto-advance to the default level on timeout.",
             "VERIFIED", merge_target="render_scene")
def level_select_dispatch(flag_mode: int, flag_password: int, input_active: int, wait_count: int) -> str:
    """[asm 8E7B-8E96] One iteration of the level-select main loop's decision.

    ``flag_mode`` = ``[0x27F6]``, ``flag_password`` = ``[0x27F7]`` (screen-request flags); ``input_active`` =
    ``[0x282d] | [0x2810]`` (a keypress); ``wait_count`` = ``[0x27F0]`` (the idle frame counter). Returns the
    screen to run, or :data:`LS_WAIT` to keep idling, or :data:`LS_AUTO_ADVANCE` once the timeout elapses."""
    if flag_mode:                                  # [asm 8E7B] cmp [0x27F6],0; jne 8EC9
        return LS_MODE_SELECT
    if flag_password:                              # [asm 8E81] cmp [0x27F7],0; jne 8EDE
        return LS_PASSWORD
    if input_active:                               # [asm 8E87] [0x282d]|[0x2810]; jne 8EC9
        return LS_MODE_SELECT
    if wait_count < _LS_TIMEOUT:                   # [asm 8E90] cmp [0x27F0],0x10E; jb 8E7B
        return LS_WAIT
    return LS_AUTO_ADVANCE                          # [asm 8E98] timeout -> default level
