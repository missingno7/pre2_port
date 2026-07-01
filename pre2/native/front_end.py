"""Native front-end flow driver — the VM-less scene state machine (cold boot -> intro -> title -> menu -> map ->
gameplay), the counterpart of ``native_frame_step`` for the NON-gameplay screens.

The recovered scenes (``pre2/recovered/oldies_screen``, ``title_image``, ``menu_scene``, ``carte``) already RENDER
(``pre2/recovered/scene.py:render_scene``). What is missing — and what this module is — is the **scene state
machine**, the "border" ``scene.py`` calls out (its line 13: *scene logic / state machine PRODUCES SceneState;
render_scene only DRAWS it*). It reproduces main's flow spine (1030:00a0):

    cold boot -> OLDIES (2505/244E) -> title -> menu (2dfa, mode select) -> map (carte, level select)
              -> level-init (013e: native_level_init) -> gameplay (0214: native_frame_step) -> level-end
              -> map / next level ...

Each scene is driven the same way: produce its ``SceneState`` from the recovered scene logic, render it, then run
its per-scene input/transition (the scene-wait ``0bbe`` = fire press+release no-timeout; the menu mode-select;
the map level-select). This is a GENERATOR (like ``native_frame_step``): it ``yield``s each displayed scene frame
so the runner presents the whole front-end animation, and hands off to ``native_frame_step`` once a level starts.

VERIFICATION: the cold-start demo (recorded from the OLDIES screen through L1->L2) is the oracle. Each scene's
recovered drive is checked tick-for-tick against the VM the same way the gameplay is (game-tick demo): seed at the
scene, inject the demo's input, assert the produced ``SceneState`` / transition matches.

STATUS: scaffolding + the scene sequence. Each scene's drive (input + transition timing + the produced SceneState)
is recovered one at a time; until a scene's drive lands it raises ``Pre2HybridGap`` (fail loud — no silent ASM
fallback, there is no VM in standalone mode). Cold boot from files (#10) provides the entry state; until then the
entry is the cold-boot snapshot (the OLDIES state), exactly as the gameplay runner bootstraps from a snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass

from dos_re.dos import _dac8
from pre2.bridge.image_scene import image_palette, render_image_scene
from pre2.bridge.input_decode import apply_ds, readers
from pre2.bridge.oldies_scene import build_oldies_scene
from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.state import DATA_SEG
from pre2.recovered.front_end_fade import fade_in_frames, fade_out_frames, palette_morph_frames
from pre2.recovered.input_decode import decode_input
from pre2.recovered.scene import (
    MODE_LINEAR, MODE_PLANAR,
    SCENE_INTRO, SCENE_MAP, SCENE_MENU, SCENE_TITLE,
)

_DS = DATA_SEG << 4

# The per-level song (indexed by [0x2d8a]) the loader loads at level start. L1 = MINES.TRK is verified from the
# menu->L1 demo; the full per-level table is a follow-up (default MINES so any level still gets music).
_LEVEL_SONGS = {0: "MINES.TRK"}


@dataclass(frozen=True)
class FrontEndScene:
    """One displayed front-end frame — a presentable descriptor the runner renders to RGB.

    Mixed-mode (the front-end uses both VGA modes), so the driver stays free of numpy / VGA rendering:
      * ``MODE_PLANAR`` (0Dh, OLDIES/menu/map): ``planes`` (4 EGA bitplanes) + ``page`` display-start offset;
      * ``MODE_LINEAR`` (13h, title artwork): ``linear`` (320×200 256-colour image).
    ``palette`` is the 256-entry 8-bit ``(r,g,b)`` DAC to display this frame with (already expanded the way
    the VGA DAC does — :func:`dos_re.dos._dac8`), so the runner just deplanarizes / indexes and blits.

    The panning planar screens (the menu/map, which scroll via the CRTC) add the fine pel-pan (``pel`` 0..7,
    from :func:`pre2.recovered.world_map.map_pan`) and the display-start ``wrap`` (``0x1FFF`` for the menu's
    0x2000-word panning window, ``0xFFFF`` = no wrap for the static OLDIES screen). The runner deplanarizes
    ``planes`` starting at ``page`` with the ``pel`` shift, wrapping plane offsets at ``wrap``."""
    mode: int
    palette: tuple                       # 256 × (r, g, b) 8-bit
    planes: tuple | None = None          # MODE_PLANAR: 4 plane buffers
    page: int = 0                        # MODE_PLANAR: display-start offset (the panned start for menu/map)
    linear: bytes | None = None          # MODE_LINEAR: 64000-byte 256-colour image
    pel: int = 0                         # MODE_PLANAR pan: fine pel-pan 0..7 (attr 0x33)
    wrap: int = 0xFFFF                   # MODE_PLANAR pan: display-start wrap (0x1FFF for the menu window)
    active_width: int = 320              # MODE_PLANAR pan: CRTC H-display width (carte narrows to 312)


def _expand_palette6(pal6: bytes) -> tuple:
    """6-bit DAC palette (768 bytes) -> 256 × (r,g,b) 8-bit, the way the VGA DAC expands it ([asm out 3C9])."""
    return tuple((_dac8(pal6[i * 3]), _dac8(pal6[i * 3 + 1]), _dac8(pal6[i * 3 + 2])) for i in range(256))

# scene-wait phases (the ASM's two busy-wait loops at 0bbe)
WAIT_PRESS = "press"
WAIT_RELEASE = "release"

# The front-end scene sequence (main 00a0). Each entry = a scene phase the state machine steps through; the value
# is the recovered-drive entry to add as it lands. Gameplay (after the map) is handed to native_frame_step.
FRONT_END_SEQUENCE = (
    SCENE_INTRO,   # OLDIES / credits (2505/244E) + the scene-wait 0bbe -> title
    SCENE_TITLE,   # title artwork + scene-wait -> menu
    SCENE_MENU,    # mode select (beginner/expert) -> map
    SCENE_MAP,     # world map / level select -> level-init -> gameplay
)


def native_scene_wait(state, phase: str) -> tuple[str, bool]:
    """[asm 0bbe] The scene-wait primitive, one poll per displayed frame: decode input (DC1) then watch the fire
    key (``[0x27e8] | [0x2832]``). Two phases mirror the ASM's two busy-wait loops — wait for the key to be
    PRESSED (0bc3-0bcd: ``or al,[0x2832]|[0x27e8]; je loop``), then RELEASED (0bcf-0bd9: ``or al,…; jne loop``) —
    then the scene transitions (the RET at 0bdb). Returns ``(new_phase, done)``; ``done`` is True the frame the
    release completes. ``phase`` starts :data:`WAIT_PRESS`.

    The ASM busy-waits (spins polling DC1) on the wall clock; the flow driver polls once per displayed frame (the
    input source — demo / live keyboard — advances per frame). Within one frame the input is constant, so the
    spin's repeated DC1 reads see the same flags — i.e. the native per-frame poll detects the same press/release
    edge at the same frame the VM's loop exits. DC1 itself is byte-exact (decode_input)."""
    rb, rw = readers(state)
    apply_ds(state, decode_input(rb, rw))                  # [asm 0bc3 / 0bcf] DC1 input decode
    fire = (rb(0x27E8) | rb(0x2832)) & 0xFF                # [asm 0bc6-0bc9 / 0bd2-0bd5] fire = [0x27e8] | [0x2832]
    if phase == WAIT_PRESS:
        return (WAIT_RELEASE if fire else WAIT_PRESS), False   # [asm 0bcd] je: loop until pressed
    return phase, fire == 0                                    # [asm 0bd9] jne: loop until released -> RET 0bdb


def native_front_end(state, dos, display_page: int, *, game_root: str):
    """Drive the whole VM-less front-end, advancing the wall-clock idle counter ``[0x27F0]`` by one 70Hz timer
    tick per displayed frame. The front-end runs the timer exactly as the VM does (its scenes spin on the retrace),
    so gameplay starts with ``[0x27F0]`` at a lived-in value rather than 0 — a value the VM never has (its timer
    has run since boot). Without it the idle player at level start picks the wrong fidget pose (a crouch instead of
    the upright stand, since the idle-fidget selector 5DC9 reads ``[0x27F0] & 0x1FF``). The inner generator
    (:func:`_native_front_end_frames`) loads the level after its last displayed frame."""
    from pre2.native.loop import native_idle_timer_tick
    for scene in _native_front_end_frames(state, dos, display_page, game_root=game_root):
        native_idle_timer_tick(state, ticks=1)                 # 1 timer tick per front-end retrace (70Hz)
        yield scene


def _native_front_end_frames(state, dos, display_page: int, *, game_root: str):
    """Drive the front-end scene state machine from the entry state, ``yield``ing each displayed scene frame, until
    a level starts — then the caller switches to ``native_frame_step`` for gameplay.

    The scene sequence (FRONT_END_SEQUENCE) mirrors main 00a0. Each scene: produce its SceneState (recovered scene
    logic), render it (render_scene), run its per-scene input/transition. As each scene's drive is recovered it is
    wired here and verified against the cold-start demo; the unrecovered ones fail loud."""
    # --- OLDIES (240a): the recovered credits render leaf + the verified scene-wait (0bbe) ---
    # Composition of two proven pieces: build_oldies_scene (rendered VM-less over the NativeGameState) +
    # native_scene_wait (verified vs the VM's 0bbe RET at present-frame 64 on the cold-start demo).
    # [asm 0b92] the OLDIES screen loads its OWN 16-colour palette (the green/yellow credits look) from the DGROUP
    # table at 0x287e (int10 AX=1012) — without it the runner shows the default EGA palette (wrong colours).
    from pre2.native.render import native_load_dac_palette
    native_load_dac_palette(state, dos, 0x287E)
    phase = WAIT_PRESS
    while True:
        planes, _ = build_oldies_scene(state, page=display_page)        # [asm 240a] credits over black
        yield FrontEndScene(MODE_PLANAR, palette=tuple(dos.vga_palette),
                            planes=tuple(bytes(p) for p in planes), page=display_page)
        phase, done = native_scene_wait(state, phase)                  # [asm 0bbe] fire press -> release
        if done:
            break

    # --- TITUS screen (912b): the first 13h title, TIMED (main 00FE-0104). Composition of three VERIFIED leaves:
    #     render_image_scene (the image, Δ=0 vs the framebuffer) + front_end_fade (the DAC fade, byte-exact vs the
    #     VM DAC) at the measured cadence: fade-IN 31 + hold 70 ([asm 9146 cx=0x46]) + fade-OUT 16. The 07c9 sound
    #     init + SAMPLE.SQZ load is an audio command (NativeAudio), not a visual frame. ---
    yield from _native_title_screen(game_root, "TITUS.SQZ", n_entries=0x10, hold=70)

    # --- PRESENT.SQZ title (9090): 02cc loads the title song PRESENTA.TRK (the FIRST music) — reproduced as an
    #     audio-command state write so the runner's NativeAudio plays it; then the "PREHISTORIK 2" title — fade-IN
    #     256 over the background, then the 911D palette MORPH 234 over the background+logo (the title's colour
    #     reveal). The morph target is the static palette at DGROUP 0xACE7. ---
    from pre2.native.audio import native_load_song
    native_load_song(state, "PRESENTA.TRK", game_root)           # [asm 02cc] the PRESENT title song (first music)
    morph_target = bytes(b & 0x3F for b in state.data[_DS + 0xACE7:_DS + 0xACE7 + 0x300])
    yield from _native_present_screen(game_root, morph_target)

    # --- FRONT.SQZ (the HUD front-panel) is stacked permanently before the sprite bank ([0x2875] 0x27be -> 0x2cd7),
    #     exactly as main() does between the titles and 2dfa. Without it the level later lands 0x519 paragraphs too
    #     low and its parallax over-reads the wrong memory. (The Python title renders don't bump [0x2875].) ---
    from pre2.native.assets import load_sqz
    load_sqz(state, "FRONT.SQZ", game_root=game_root)             # [asm 107B] permanent front-panel load
    # --- 2dfa: decode the shared SPRITES.SQZ sprite bank into the game state (the title is still on screen — no new
    #     visible frame). This is the bank the world-map + gameplay need. Verified byte-exact (native_build_sprite_bank,
    #     [[pre2-front-end-flow]]); composes from the post-FRONT state ([0x2875] 0x2cd7 -> 0x5cc1, the VM's level seg). ---
    from pre2.native.sprite_bank import native_build_sprite_bank
    native_build_sprite_bank(state, game_root=game_root)

    # --- 8e45: the "press 1/2" difficulty screen (MENU.SQZ = resource 8, a 13h image) faded in over 0xA0 DAC
    #     entries ([asm 8e67 cl=0xA0] -> 919f), then held while the dispatch waits for a choice. Pixel-exact vs the
    #     VM. The dispatch flags [0x27f6]/[0x27f7] ARE the '1'/'2' key-table entries (0x27f4 + scancode 2/3); fire =
    #     [0x282d]|[0x2810] (space/enter); auto-advance after 0x10E idle frames. ---
    from pre2.recovered.world_map import LS_PASSWORD, LS_WAIT, level_select_dispatch
    menu_img = render_image_scene("MENU.SQZ", game_root)
    menu_fade = fade_in_frames(image_palette("MENU.SQZ", game_root), 0xA0)   # [asm 919f] black -> the menu palette
    for p6 in menu_fade:
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=menu_img)
    held8 = _expand_palette6(menu_fade[-1])
    rb, _ = readers(state)
    wait = 0
    choice = LS_WAIT
    while choice == LS_WAIT:                                                 # [asm 8e7b] the dispatch wait loop
        choice = level_select_dispatch(rb(0x27F6), rb(0x27F7), rb(0x282D) | rb(0x2810), wait)  # 1 / 2 / fire / timeout
        if choice != LS_WAIT:
            break
        yield FrontEndScene(MODE_LINEAR, palette=held8, linear=menu_img)     # hold the screen, poll again next frame
        wait += 1

    # --- 96d5: the chosen 0Dh scrolling world-map — password entry (for '2') or mode-select (for '1'/fire). Renders
    #     pixel-exact VM-less (the caveman-tiled map scrolling with the text stamped on top). Mode-select confirm
    #     (fire) selects level 1 and returns here; password entry still fails loud (code entry not wired). ---
    native_load_song(state, "CODE.TRK", game_root)              # the mode-select song (starts on entering 96d5, not the menu)
    yield from _native_menu_map(state, dos, game_root, "password" if choice == LS_PASSWORD else "mode_select")

    # --- 9520 CARTE: the world-map scroll-in the real flow shows between the mode-select and the loader (main 0x01A8).
    #     Loads MAP.SQZ (the map master), stamps the per-level 'you are here' marker (the player's position) onto it =
    #     the de-planarize [0x667a]:[0x62da] (byte-exact vs the VM master), then scrolls the map in via the recovered +
    #     byte-exact build_carte_page until fire, then hands to the loader. ---
    yield from _native_carte(state, dos, game_root)

    # --- the map selected a level ([0x2d8a]); the loader (main 01A5..01D2). native_level_start is VERIFIED byte-exact
    #     vs the pure-ASM oracle's gameplay-entry seed (every core gameplay table identical; see [[pre2-level-init-island]]). ---
    from pre2.native.level_init import native_level_start
    native_load_song(state, _LEVEL_SONGS.get(state.data[_DS + 0x2D8A], "MINES.TRK"), game_root)  # level song (plays at start)
    native_level_start(state, game_root=game_root)            # [asm 013e..0155] load + init + level-start (lives, etc.)
    state.data[_DS + 0x27F4:_DS + 0x27F4 + 0x80] = bytes(0x80)  # clear the residual key table (the DC1 raw keys)
    return                                                     # the level is loaded -> the caller runs native_frame_step


_ZERO_SINE = bytes(0x100)          # the password/mode-select map does not bounce (row stays 0), so the sine is unused


def _native_menu_map(state, dos, game_root: str, kind: str):
    """[asm 96D5] Drive the 0Dh scrolling world-map screen VM-less (``kind`` = ``"password"`` 9985 / ``"mode_select"``
    991F). Yields :class:`FrontEndScene` (MODE_PLANAR) frames — the caveman-tiled map (MOTIF.SQZ) scrolling left, with
    the screen's text (``ENTER CODE`` + the code, or ``MODE`` + BEGINNER/EXPERT) stamped on top each frame via
    ``draw_string`` against the bit-rotated font. Verified pixel-exact vs the VM (all planes, every frame). Raises
    ``Pre2HybridGap`` on the exit — the carte + level-init handoff (the path to gameplay) is the next island."""
    from pre2.codecs.sqz import unpack_sqz
    from pre2.native.assets import resolve_game_path
    from pre2.native.render import native_load_dac_palette
    from pre2.recovered.menu_scene import MenuScenePage, build_shifted_font
    from pre2.recovered.present import scroll_shift_frame
    from pre2.recovered.text import draw_string
    from pre2.recovered.world_map import (MapCamera, map_camera_update, map_page_flip, map_pan,
                                          mode_select_input, mode_select_text_runs, password_text_runs)
    rb, rw = readers(state)
    native_load_dac_palette(state, dos, 0xB118, 0x10)             # [asm 97A5] the map's 16-colour palette
    page = MenuScenePage()
    page.seed(unpack_sqz(resolve_game_path(game_root, "MOTIF.SQZ").read_bytes())[:0x3E80])  # [asm 96EC/9718] planes 0,1
    fseg = rw(0x3D)                                               # the font segment ([0x3d])
    font = build_shifted_font(bytes(state.data[(fseg << 4):(fseg << 4) + 0x3000]))          # [asm 972E] shift copies

    def text_runs():
        return password_text_runs() if kind == "password" else mode_select_text_runs(rb(0xB197))

    def cstr(off):                                               # the NUL-terminated string at DS:off
        end = state.data.index(0, _DS + (off & 0xFFFF))
        return bytes(state.data[_DS + (off & 0xFFFF):end])

    cam = MapCamera(0, 0, 2, 0, 0, 0)                            # [asm 978D] phase seeds at 2
    prev_page = 0
    prev_arrow = False                                          # edge-detect the arrow for the BEGINNER/EXPERT toggle
    bounce = state.data[(0x1030 << 4) + 5] != 0                # [asm 9AF5] cs:[5] (=3): the vertical sine-bounce is ON
    sine = bytes(state.data[_DS + 0x6F90:_DS + 0x6F90 + 0x100])  # [asm 9B00] the row-bounce sine table (DGROUP 0x6f90)
    pal = tuple(dos.vga_palette)

    def scene(planes, page_off, pel):
        return FrontEndScene(MODE_PLANAR, palette=pal, planes=tuple(bytes(p) for p in planes),
                             page=page_off, pel=pel, wrap=0x1FFF)

    yield scene(page.planes, 0, 0)                               # frame 0 = the seeded page
    while True:
        cam = map_camera_update(cam, bounce=bounce, sine_table=sine)   # [asm 9AE0] scroll left 4/frame + row sine-bounce
        ds, _ = map_pan(cam.x, cam.row)                          # [asm 97A8] display-start from the CURRENT x
        pel = cam.prev_x & 7                                    # the pel-pan LAGS one frame (= prev_x & 7)
        page_draw, page_clear = map_page_flip(ds, prev_page)     # [asm 97CA] page_draw=ds, page_clear=prev
        # VISUAL APPROXIMATION (not byte-exact — see [[pre2-front-end-flow]]): the VM double-buffers the text across
        # the two ring pages, which combined with the vertical sine-bounce we don't yet reproduce exactly, so the
        # text would ghost/smear. Clear the text planes (2|3) each frame and re-stamp fresh so it stays clean. The
        # BACKGROUND (planes 0,1) + the bounce ARE byte-exact; only this text layer is an approximation for now.
        page.planes[2][:0x2000] = bytes(0x2000)
        page.planes[3][:0x2000] = bytes(0x2000)
        for run in text_runs():                                 # [asm callback 9985/991F] stamp the text
            draw_string(page.planes, cstr(run.addr), font, cam.blit_off, run.pen, run.advance, page_clear, page_draw)
        scroll_shift_frame(page.planes, cam.prev_x, cam.x, cam.row, cam.prev_row, page_clear)   # [asm 9804] H + V (bounce) pan
        prev_page = page_draw
        yield scene(page.planes, ds, pel)
        apply_ds(state, decode_input(rb, rw))                  # DC1: host keys -> the FSM arrow/fire flags
        if kind == "mode_select":                              # [asm 994E] an arrow toggles BEGINNER <-> EXPERT
            arrow_sc = (0x48 if rb(0x27EA) else 0x50 if rb(0x27EB) else       # up / down
                        0x4D if rb(0x27EC) else 0x4B if rb(0x27ED) else 0)    # right / left
            if arrow_sc and not prev_arrow and mode_select_input(arrow_sc, False).toggle:
                state.data[_DS + 0xB197] ^= 1                  # flip the selection (the text re-renders next frame)
            prev_arrow = bool(arrow_sc)
        if (rb(0x27E8) | rb(0x2832)) != 0:                      # fire = confirm
            if kind == "mode_select":                          # [asm 8F12/8F18/8ED7] commit the difficulty, start L1
                state.data[_DS + 0xB198] = state.data[_DS + 0xB197]
                state.data[_DS + 0x83D] = state.data[_DS + 0xB197]
                state.data[_DS + 0x2D8A] = 0                    # BEGINNER/EXPERT both begin at level 1
                return                                          # (the 965a carte scroll-in is a deferred visual)
            raise Pre2HybridGap(                                # password entry (accumulate + validate) not wired yet
                f"native front-end: the 96d5 password screen renders VM-less (pixel-exact). Next: the code entry "
                f"(accumulate hex -> validate -> level) + the 965a carte visual (#14).")


def _native_carte(state, dos, game_root: str):
    """[asm 9520] The CARTE world-map scroll-in shown between the mode-select and the level load.

    Loads ``MAP.SQZ`` (the 4-plane map master at ``[0x2875]``), stamps the per-level 'you are here' marker (the
    player's position on the world map) onto it — the 9520 head de-planarizes it from ``[0x667a]:[0x62da]`` via
    ``stamp_carte_marker``, byte-exact vs the VM's stamped master (the 0.6% overlay) — then reveals it column-by-column
    with the recovered + byte-exact ``build_carte_page`` (965A) as the CRTC pans (``carte_display``). MAP.SQZ is a
    TRANSIENT load (the loader reloads over it), so the load pointer ``[0x2875]`` is restored afterwards. Fire (once
    released after the mode-select confirm) advances to the loader."""
    from pre2.native.assets import load_sqz
    from pre2.native.render import native_load_dac_palette
    from pre2.recovered.carte import (build_carte_page, carte_display, carte_marker_offset,
                                       stamp_carte_marker, SCROLL_START)
    rb, rw = readers(state)
    saved_top = rw(0x2875)                                       # [asm 9530] MAP.SQZ loads at the top...
    seg = load_sqz(state, "MAP.SQZ", game_root=game_root)
    state.data[_DS + 0x2875] = saved_top & 0xFF                  # ...but is transient — restore the load pointer so
    state.data[_DS + 0x2876] = (saved_top >> 8) & 0xFF          # the loader stacks the level exactly where the VM does
    master = bytes(state.data[(seg << 4):(seg << 4) + 0xFA00])   # the 4-plane map master (planes @0/3E80/7D00/BB80)
    # [asm 9543-95CD] stamp the per-level 'you are here' marker (the player's caveman on the map) into the master.
    lv = rb(0x2D8A)                                              # the level index picks its map (x,y) + the marker
    dims = rw(0x7522); mw = (dims & 0xFF) >> 3; mh = dims >> 8   # marker size (bytes wide / rows) [asm 9562-956A]
    di = carte_marker_offset(rw(0xB148 + lv * 4), rw(0xB14A + lv * 4))
    msrc = (rw(0x667A) << 4) + rw(0x62DA)                        # [0x667a]:[0x62da] — mask + 4 colour planes
    marker = bytes(state.data[msrc:msrc + 5 * mw * mh])
    master = stamp_carte_marker(master, marker, di, mw, mh)
    from pre2.native.audio import native_load_song
    native_load_song(state, "CARTE.TRK", game_root)             # the carte song
    native_load_dac_palette(state, dos, 0xB0E8, 0x10)           # [asm 95EB] the carte's 16-colour palette
    pal = tuple(dos.vga_palette)
    scroll_x = SCROLL_START                                     # [asm 95E2] carte begins at scroll 8
    prev_fire = True                                            # require the mode-select fire to RELEASE first
    while True:
        planes = build_carte_page(master, scroll_x)            # [asm 9613] reveal the map up to scroll_x
        ds, pel = carte_display(scroll_x)                      # [asm 97A8-style] CRTC display start + pel pan
        yield FrontEndScene(MODE_PLANAR, palette=pal, planes=tuple(bytes(p) for p in planes),
                            page=ds, pel=pel, wrap=0x1FFF)
        apply_ds(state, decode_input(rb, rw))
        if scroll_x < 639:                                     # scroll the map in +1/frame (the VM's [0xb19d] rate)
            scroll_x += 1
        fire = (rb(0x27E8) | rb(0x2832)) != 0
        if fire and not prev_fire:                             # a fresh fire press -> confirm -> the loader
            return
        prev_fire = fire


def _native_title_screen(game_root: str, name: str, *, n_entries: int, hold: int):
    """[asm 912b/9090 -> 919f] Drive one 13h title screen as a sequence of :class:`FrontEndScene` frames: the
    image is decoded+composited once (``render_image_scene``), then the DAC fade-IN, the hold, and the fade-OUT
    are the verified :mod:`pre2.recovered.front_end_fade` palettes applied to it.

    ``n_entries`` = the DAC entry count the fade ramps (TITUS ``0x10``, PRESENT ``0xFF`` = ``cs:[9153]``); ``hold``
    = the steady retrace count between fade-in and fade-out ([asm 9146 ``cx``])."""
    image = render_image_scene(name, game_root)                         # [asm 91A4/9090] 64000-byte 13h image
    pal6 = image_palette(name, game_root)                               # [asm 91D5 DS:si] the asset's target palette
    fade_in = fade_in_frames(pal6, n_entries)                           # [asm 919F loop] black -> target
    held = fade_in[-1]                                                  # the converged held palette
    fade_out = fade_out_frames(held, n_entries)                         # [asm 9286 loop] target -> black
    for p6 in fade_in:                                                  # [asm 919F] fade-in retrace frames
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=image)
    held8 = _expand_palette6(held)
    for _ in range(hold):                                               # [asm 9146-914C] steady hold
        yield FrontEndScene(MODE_LINEAR, palette=held8, linear=image)
    for p6 in fade_out:                                                 # [asm 914E -> 9286] fade-out retrace frames
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=image)


def _native_present_screen(game_root: str, morph_target: bytes):
    """[asm 9090] Drive the PREHISTORIK-2 (PRESENT.SQZ) title as :class:`FrontEndScene` frames: the background
    fades IN (919F, n=0xFF), the logo-top is composited on, then the palette MORPHS toward ``morph_target``
    (911D) — the title's colour reveal. Two image variants: the fade-in shows the background only (the logo is
    copied AFTER 919F returns), the morph shows background+logo.

    ``morph_target`` is the 6-bit palette at DGROUP ``0xACE7`` (read from the game state)."""
    background = render_image_scene("PRESENT.SQZ", game_root, with_logo=False)   # [asm 91A4] fade-in target
    full = render_image_scene("PRESENT.SQZ", game_root, with_logo=True)          # [asm 9090 logo copy] morph image
    pal6 = image_palette("PRESENT.SQZ", game_root)
    fade_in = fade_in_frames(pal6, 0xFF)                               # [asm 919F] black -> background palette
    held = fade_in[-1]
    for p6 in fade_in:
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=background)
    # the 911D retrace counter enters with the BL the fade-in left = the green of its last entry (entry 0xFE)
    phase = held[(0xFF - 1) * 3 + 1]
    morph = palette_morph_frames(held, morph_target, initial_phase=phase)         # [asm 911D] colour reveal
    for p6 in morph:
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=full)
    # [asm 9286] fade the revealed title back OUT to black before the menu loads — the title-to-menu transition
    # is fade-OUT then fade-in (the menu's own 919f mode-set clears instantly, so without this the title just
    # snaps to black instead of fading). The whole 256-colour title fades to black (n=0x100), a ~120-frame ramp
    # (the VM's DAC brightness falls from full to black over ~112 retraces before the menu fades in).
    for p6 in fade_out_frames(morph[-1], 0x100):
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=full)
