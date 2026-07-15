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

from pre2.native.vga import _dac8
from pre2.views.image_scene import image_palette, render_image_scene
from pre2.views.memory_adapter import apply_ds, readers
from pre2.views.oldies_scene import build_oldies_scene
from pre2.gaps import Pre2ExpertEater, Pre2HybridGap
from pre2.views.dgroup_view import LoaderGlobals, PasswordScreenView, PlayerGlobals
from pre2.views.tables import ByteTable
from pre2.native.state import DATA_SEG
from pre2.recovered.front_end_fade import fade_in_frames, fade_out_frames, palette_morph_frames
from pre2.recovered.input_decode import decode_input
from pre2.native.dgroup_offsets import (
    CARTE_MARKER_TABLE, KEY_TABLE, MENU_MORPH_SRC, ROW_SINE_TABLE)
from pre2.recovered.scene import (
    MODE_LINEAR, MODE_PLANAR,
    SCENE_INTRO, SCENE_MAP, SCENE_MENU, SCENE_TITLE,
)

_DS = DATA_SEG << 4


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
    screen: str = ""                     # front-end screen id for the touch host's per-screen input mapping
    #                                      ("menu" = the press-1/2 difficulty screen -> a tap presses '1'). Empty
    #                                      elsewhere (a tap is fire). Presentation metadata only; game-logic-free.
    game_paced: bool = False             # True = this frame is a game-TICK-rate scene (the VM presents it via
    #                                      44FB -> the 3-retrace 1C6F wait, ~23.33Hz), NOT the 70Hz retrace of
    #                                      the static front-end screens. The attract title animation is such a
    #                                      scene; the runner must pace it at the game rate or it plays 3x fast.
    enh: tuple | None = None             # ENHANCED-presentation payload for the scrolling map screens (the
    #                                      faithful raster above stays authoritative; a runner MAY instead render
    #                                      this wide + display-rate-smooth): ("carte", master_bytes, scroll_x) —
    #                                      the 640px stamped map master + the reveal position; ("menu",
    #                                      motif_bytes, cam_x, cam_row) — the 320x200 bg pattern (planes 0,1
    #                                      source) + the scroll phase (text stays in planes 2|3 of ``planes``).


def _expand_palette6(pal6: bytes) -> tuple:
    """6-bit DAC palette (768 bytes) -> 256 × (r,g,b) 8-bit, the way the VGA DAC expands it ([asm out 3C9])."""
    return tuple((_dac8(pal6[i * 3]), _dac8(pal6[i * 3 + 1]), _dac8(pal6[i * 3 + 2])) for i in range(256))


def _dac_fade_palettes(target_dac, *, into: bool, steps: int = 0x10):
    """Yield ``steps`` intermediate 256-entry DAC palettes ramping between black and ``target_dac`` (the demo
    level's live palette) — ``into=True`` fades IN (black -> target), ``into=False`` fades OUT (target -> black).
    The attract's [asm 8EB3/8F29] 9286 fade-to-black transitions bracket the demo; native snapped instantly."""
    tgt = tuple(target_dac)
    n = len(tgt)
    for i in range(1, steps + 1):
        k = i / steps
        f = k if into else (1.0 - k)
        yield tuple((int(r * f), int(g * f), int(b * f)) for (r, g, b) in tgt) if n else tgt


def _planar_fade_out(state, pal6_off: int, planes, page: int, pel: int, wrap: int = 0x1FFF, enh=None):
    """[asm 9286] Fade a 0Dh PLANAR screen (menu-map / carte) OUT to black before the next screen — yields
    :class:`FrontEndScene` frames holding the current planes while the 16-colour DAC dims to black.

    The VM runs this DAC fade-out (the byte-exact 9286 ramp, reused from the title screens) between the
    mode-select and the carte, and between the carte and the level load; native snapped instantly. ``pal6_off``
    is the DGROUP offset of the screen's 16-entry 6-bit palette (menu-map 0xB118 / carte 0xB0E8). The planes are
    frozen (a DAC fade changes only the palette), so we re-yield them with each fading DAC snapshot. ``enh``
    carries the caller's frozen enhanced payload so a wide presentation fades wide (not snapping to 320)."""
    pal6 = bytes(state.data[_DS + pal6_off:_DS + pal6_off + 0x10 * 3])   # the screen's 16-colour 6-bit palette
    frozen = tuple(bytes(p) for p in planes)
    for snap in fade_out_frames(pal6, 0x10):                             # [asm 9286] 16-entry DAC ramp to black
        yield FrontEndScene(MODE_PLANAR, palette=_expand_palette6(snap), planes=frozen, page=page, pel=pel,
                            wrap=wrap, enh=enh)

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
    g = PlayerGlobals(state)
    apply_ds(state, decode_input(rb, rw))                  # [asm 0bc3 / 0bcf] DC1 input decode
    fire = (g.in_fire | g.fire_alt) & 0xFF                  # [asm 0bc6-0bc9 / 0bd2-0bd5] fire = [0x27e8] | [0x2832]
    if phase == WAIT_PRESS:
        return (WAIT_RELEASE if fire else WAIT_PRESS), False   # [asm 0bcd] je: loop until pressed
    return phase, fire == 0                                    # [asm 0bd9] jne: loop until released -> RET 0bdb


def _skippable_intro(scenes, state, *, enabled: bool):
    """Wrap an intro title generator so a fire-key press can skip it (jumping the flow straight to the menu).

    Not in the VM: the real TITUS / PRESENT titles play uninterruptibly. So when ``enabled`` is False this just
    re-``yield``s the scenes with NO input polling — byte-exact. When on (the touch default), it decodes DC1 once
    per displayed frame and stops early on a FRESH fire press; the key must be seen released first, so a fire held
    over from the OLDIES skip doesn't instantly abort the title. Returns True if it was skipped, so the caller can
    drop the remaining intro screens too (``skipped = yield from _skippable_intro(...)``)."""
    if not enabled:
        yield from scenes
        return False
    rb, rw = readers(state)
    g = PlayerGlobals(state)
    armed = False                                              # only skip on a fresh press (wait for a release first)
    for scene in scenes:
        yield scene
        apply_ds(state, decode_input(rb, rw))                 # DC1 input decode (same read as native_scene_wait)
        fire = (g.in_fire | g.fire_alt) & 0xFF                 # fire = [0x27e8] | [0x2832] (space/enter)
        if not fire:
            armed = True
        elif armed:
            return True                                        # fresh fire press -> skip the rest of the intro
    return False


def _native_attract(state, dos, game_root: str):
    """[asm 8E98] The menu-idle ATTRACT: show the PREHISTORIK-2 title (the VM's resource-0xC blit), then the CARTE
    (as a normal level load does), then play the demo's level ([0x83E]) in DEMO-PLAYBACK input mode ([0x2879]=1).
    The DS:0x3F demo buffer drives the caveman; any key ([0x2874]) or the 0x55AA end sentinel makes DC1 set [0x6BE5]
    (a game-over-style death), which kills the player and returns to the menu. Yields each frame; on the end
    transition it clears the demo state and returns, and the caller re-shows the menu. The title + carte run at the
    70Hz front-end rate; only the gameplay ([0x2879]=1) runs at the game rate (play_native picks the fps)."""
    from pre2.gaps import (Pre2CaveTeleport, Pre2GameOverTransition, Pre2HybridGap,
                                          Pre2LevelEndTransition, Pre2RespawnTransition)
    from pre2.native.attract_title import native_attract_title
    from pre2.native.audio import native_level_song_name, native_load_song
    from pre2.native.level_init import native_level_start
    from pre2.native.render import native_load_level_palette
    from pre2.native.runtime import native_frame_step
    d = state.data
    g = PlayerGlobals(state)
    g.level = g.attract_level                                     # [asm 8ebb] the demo's level ([0x83E], normally 0 = L1)
    g.mode_copy = g.attract_mode                                  # [asm 8ec4]
    # --- [asm 8F2D] the animated MENU2 title (caveman runs across the PREHISTORIK-2 jungle chased by a dino),
    #     then the carte, exactly as a normal level start shows them. Pixel-exact vs the VM (attract_title;
    #     119 frames, 0 diff). Replaces the earlier static-title stand-in (user: "should be a game-like screen
    #     with a running character"). [0x2879] stays 0 — the animation is a scripted scene, NOT the demo. ---
    yield from native_attract_title(state, game_root)        # [asm 8f2d] the running-caveman title animation
    yield from _native_carte(state, dos, game_root)          # [asm 9520] the world-map scroll-in (auto-advances)
    # --- now the gameplay demo ---
    g.input_source = 1                                       # [asm 8eb6] demo-playback input mode
    g.demo_ptr = 0; g.demo_byte = 0; g.demo_cnt = 0               # reset the demo cursor + current byte/count
    g.pending_key = 0                                             # no pending key yet
    g.respawn_state = 0; g.end_signal = 0; g.level_end_mode = 0   # clear the death/end selectors
    d[_DS + KEY_TABLE:_DS + KEY_TABLE + 0x80] = bytes(0x80)         # clear the residual key table
    native_load_song(state, native_level_song_name(state), game_root)
    native_level_start(state, game_root=game_root)           # load the demo's level
    native_load_level_palette(state, dos)                    # [asm 0ba0] the per-level DAC palette (else the demo
    #                                                          renders under the leftover MENU palette — wrong colours)
    disp = g.display_page
    last = None                                              # (planes, page) of the last demo frame -> fade-out over it
    first = True
    try:
        while True:
            for planes, page in native_frame_step(state, dos, disp, game_root=game_root):
                frozen = tuple(bytes(p) for p in planes)
                if first:                                    # [asm 8EB3 fade-to-black -> the demo] fade IN from black
                    first = False                            #   over the demo's first rendered frame
                    for pal in _dac_fade_palettes(dos.vga_palette, into=True):
                        yield FrontEndScene(MODE_PLANAR, palette=pal, planes=frozen, page=page, game_paced=True)
                yield FrontEndScene(MODE_PLANAR, palette=tuple(dos.vga_palette),
                                    planes=frozen, page=page, game_paced=True)
                last = (frozen, page)
            disp = g.display_page
    except (Pre2GameOverTransition, Pre2LevelEndTransition, Pre2RespawnTransition,
            Pre2CaveTeleport, Pre2HybridGap):
        pass                                                 # any end/gap in the idle demo -> back to the menu
    if last is not None:                                     # fade the demo OUT to black before re-showing the menu
        for pal in _dac_fade_palettes(dos.vga_palette, into=False):
            yield FrontEndScene(MODE_PLANAR, palette=pal, planes=last[0], page=last[1], game_paced=True)
    g.input_source = 0                                       # back to live keyboard input
    g.demo_ptr = 0
    g.respawn_state = 0; g.end_signal = 0; g.level_end_mode = 0
    d[_DS + KEY_TABLE:_DS + KEY_TABLE + 0x80] = bytes(0x80)


def native_front_end(state, dos, display_page: int, *, game_root: str, intro_skippable: bool = False):
    """Drive the whole VM-less front-end, advancing the wall-clock idle counter ``[0x27F0]`` by one 70Hz timer
    tick per displayed frame. The front-end runs the timer exactly as the VM does (its scenes spin on the retrace),
    so gameplay starts with ``[0x27F0]`` at a lived-in value rather than 0 — a value the VM never has (its timer
    has run since boot). Without it the idle player at level start picks the wrong fidget pose (a crouch instead of
    the upright stand, since the idle-fidget selector 5DC9 reads ``[0x27F0] & 0x1FF``). The inner generator
    (:func:`_native_front_end_frames`) loads the level after its last displayed frame.

    ``intro_skippable`` (an opt-in; default for touch): let a fire-key press during the TITUS / PREHISTORIK-2
    title screens skip straight to the menu. OFF = byte-accurate (the titles play with no input polling)."""
    from pre2.native.loop import native_idle_timer_tick
    for scene in _native_front_end_frames(state, dos, display_page, game_root=game_root,
                                          intro_skippable=intro_skippable):
        native_idle_timer_tick(state, ticks=1)                 # 1 timer tick per front-end retrace (70Hz)
        yield scene


def _native_front_end_frames(state, dos, display_page: int, *, game_root: str, intro_skippable: bool = False):
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

    # --- 07c9 sound init (after OLDIES): load the SFX sample bank (SAMPLE.SQZ) so the runner PLAYS effects. It's
    #     audio-command state (the SFX table + [0x0B59] segment + the digital-device flag), not a visual frame. ---
    from pre2.native.audio import native_load_sfx_bank
    native_load_sfx_bank(state, game_root)

    # --- TITUS screen (912b): the first 13h title, TIMED (main 00FE-0104). Composition of three VERIFIED leaves:
    #     render_image_scene (the image, Δ=0 vs the framebuffer) + front_end_fade (the DAC fade, byte-exact vs the
    #     VM DAC) at the measured cadence: fade-IN 31 + hold 70 ([asm 9146 cx=0x46]) + fade-OUT 16. ---
    # The intro titles (TITUS, then the PREHISTORIK-2 logo) play through normally, but when ``intro_skippable``
    # is on (default for touch/Android; OFF for the byte-accurate default) a fire-key press during either title
    # skips straight to the menu. A skip during TITUS drops the PRESENT title too. When OFF the titles play with
    # NO input polling at all — byte-exact to the VM, which never checks for a skip here.
    skipped = yield from _skippable_intro(
        _native_title_screen(game_root, "TITUS.SQZ", n_entries=0x10, hold=70), state, enabled=intro_skippable)

    if not skipped:
        # --- PRESENT.SQZ title (9090): 02cc loads the title song PRESENTA.TRK (the FIRST music) — reproduced as an
        #     audio-command state write so the runner's NativeAudio plays it; then the "PREHISTORIK 2" title — fade-IN
        #     256 over the background, then the 911D palette MORPH 234 over the background+logo (the title's colour
        #     reveal). The morph target is the static palette at DGROUP 0xACE7. (native_menu_flow reloads the song.) ---
        from pre2.native.audio import native_load_song
        native_load_song(state, "PRESENTA.TRK", game_root)       # [asm 02cc] the PRESENT title song (first music)
        morph_target = bytes(b & 0x3F for b in state.data[_DS + MENU_MORPH_SRC:_DS + MENU_MORPH_SRC + 0x300])
        yield from _skippable_intro(
            _native_present_screen(game_root, morph_target), state, enabled=intro_skippable)

    # --- FRONT.SQZ (the HUD front-panel) is stacked permanently before the sprite bank ([0x2875] 0x27be -> 0x2cd7),
    #     exactly as main() does between the titles and 2dfa. Without it the level later lands 0x519 paragraphs too
    #     low and its parallax over-reads the wrong memory. (The Python title renders don't bump [0x2875].) ---
    from pre2.native.assets import load_sqz
    _front_seg = load_sqz(state, "FRONT.SQZ", game_root=game_root)  # [asm 107B] permanent front-panel load
    LoaderGlobals(state).fg_bank = _front_seg                      # [asm 0123] mov [0x3b],ax — the FOREGROUND tile-gfx
    #                                                                 bank the front pass (3721) reads; missing = no foliage
    # --- 2dfa: decode the shared SPRITES.SQZ sprite bank into the game state (the title is still on screen — no new
    #     visible frame). This is the bank the world-map + gameplay need. Verified byte-exact (native_build_sprite_bank,
    #     [[pre2-front-end-flow]]); composes from the post-FRONT state ([0x2875] 0x2cd7 -> 0x5cc1, the VM's level seg). ---
    from pre2.native.sprite_bank import native_build_sprite_bank
    native_build_sprite_bank(state, game_root=game_root)
    ldr = LoaderGlobals(state)                                          # [asm 0129-012c] mov ax,[0x2875]; mov [0x39],ax
    ldr.reset_base = ldr.load_top                                       # save the post-front-end load-top as the per-level
    #                                                                     allocation RESET base (restart frees to here)

    # --- 8e45 onward (press-1/2 menu -> mode-select/password map -> carte -> loader): shared with the
    #     GAME-OVER restart (main 011C re-enters the flow HERE — 447d -> 8e45 -> carte -> level reload). ---
    yield from native_menu_flow(state, dos, game_root)
    return                                                     # the level is loaded -> the caller runs native_frame_step


def native_menu_flow(state, dos, game_root: str):
    """[asm 8e45..01D2] The front-end from the press-1/2 MENU through the loaded level: menu (+ the idle
    ATTRACT loop), the mode-select/password map, the CARTE, then the loader (native_level_start — the FRESH
    level-start block runs: lives/BONUS/utensils reset, exactly the VM's fresh-start ax!=0 path). Shared by
    the cold boot AND the game-over restart (main 011C re-enters the front-end at this screen)."""
    from pre2.native.audio import native_load_song
    native_load_song(state, "PRESENTA.TRK", game_root)   # [asm 015a/0107 ax=3] the title/MENU song — the shared
    #   restart path (main 0x141) reloads it before 8e45, so a return here from THE END (FINAL.TRK) or the game-over
    #   scene (BOULA.TRK) switches back to the menu music. Idempotent on the cold boot (the PRESENT title just set it).
    # --- [asm 0141..0155] the FRESH-start block runs BEFORE the menu (every 8E45 entry: the cold start and the
    #     0199/012F restarts all pass through it): lives=2, BONUS-letters/utensils masks cleared, damage reset.
    #     native_level_start re-applies the same values at the load (outcome-identical), but writing them HERE
    #     matches the VM's state at menu entry byte-for-byte (the front-end transition witness reads it). ---
    g = PlayerGlobals(state)
    g.lives = 2                                          # [asm 0141]
    g.bonus_letters = 0                                  # [asm 0146] BONUS-letters mask
    g.attack_v19 = 0x14                                  # [asm 014B] damage/energy
    g.attack_phase = 0                                   # [asm 0150]
    g.utensils_mask = 0                                  # [asm 0155] utensils mask
    # --- 8e45: the "press 1/2" difficulty screen (MENU.SQZ = resource 8, a 13h image) faded in over 0xA0 DAC
    #     entries ([asm 8e67 cl=0xA0] -> 919f), then held while the dispatch waits for a choice. Pixel-exact vs the
    #     VM. The dispatch flags [0x27f6]/[0x27f7] ARE the '1'/'2' key-table entries (0x27f4 + scancode 2/3); fire =
    #     [0x282d]|[0x2810] (space/enter); auto-advance after 0x10E idle frames. ---
    from pre2.recovered.world_map import LS_AUTO_ADVANCE, LS_PASSWORD, LS_WAIT, level_select_dispatch
    menu_img = render_image_scene("MENU.SQZ", game_root)
    menu_fade = fade_in_frames(image_palette("MENU.SQZ", game_root), 0xA0)   # [asm 919f] black -> the menu palette
    while True:                                                             # the MENU <-> ATTRACT <-> CODE loop [asm 8e45]
        for p6 in menu_fade:                                                # fade the menu in (first entry + after each return)
            yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=menu_img, screen="menu")
        held8 = _expand_palette6(menu_fade[-1])
        wait = 0
        choice = LS_WAIT
        while choice == LS_WAIT:                                            # [asm 8e7b] the dispatch wait loop
            choice = level_select_dispatch(g.key_1_latch, g.key_2_latch, g.fire_latch | g.fire_space, wait)  # 1/2/fire/timeout
            if choice != LS_WAIT:
                break
            # screen="menu": the touch host maps a TAP here to '1' (beginner mode-select), not fire — matches the
            # real key AND keeps the tap's fire from carrying into the mode-select map and auto-confirming it.
            yield FrontEndScene(MODE_LINEAR, palette=held8, linear=menu_img, screen="menu")
            wait += 1
        if choice == LS_AUTO_ADVANCE:
            yield from _native_attract(state, dos, game_root)              # [asm 8E98] idle timeout -> attract, then re-loop
            continue
        # --- 96d5: the chosen 0Dh scrolling world-map — password entry (for '2') or mode-select (for '1'/fire).
        #     Pixel-exact VM-less (the caveman-tiled map scrolling with the text stamped on top). ---
        native_load_song(state, "CODE.TRK", game_root)          # the map song (starts on entering 96d5, not the menu)
        if choice == LS_PASSWORD:
            PlayerGlobals(state).level = 0xFF                   # [asm 8ee9] no level chosen yet (the match writes it)
            yield from _native_menu_map(state, dos, game_root, "password")
            if PlayerGlobals(state).level & 0x80:               # [asm 8ef1-8ef8] fire with NO valid code -> back to
                continue                                        #   the press-1/2 menu (jmp 8e45), NOT stuck on ----
            break                                               # a matched code chose its level -> the carte/loader
        yield from _native_menu_map(state, dos, game_root, "mode_select")
        break

    yield from native_carte_and_load(state, dos, game_root)
    return                                                     # the level is loaded -> the caller runs native_frame_step


def native_carte_and_load(state, dos, game_root: str):
    """[asm 9520 CARTE + main 01A5..01D2] Show the world-map scroll-in for the CHOSEN level, then load and start it.

    The level ([0x2d8a]) and mode ([0xB197]) must already be committed — by the mode-select map, a password match,
    or the mobile CONTINUE screen, which all write the SAME two bytes a valid password does. Yields the carte
    FrontEndScene frames; on return the level is loaded and ready for ``native_frame_step``. Factored out of
    :func:`native_menu_flow` so a host that picks a level directly (the CONTINUE screen) reaches the loader without
    re-driving the press-1/2 menu."""
    # --- [asm 0163] the EXPERT-EATER gate: main checks it AFTER the level is committed (the menu / a password match /
    #     the CONTINUE picker all wrote [0x2d8a]/[0xB197] already) but BEFORE the carte (9520) and the loader (447d).
    #     A BEGINNER who chose level 8/9 (the penguin is expert-only) is shown CASTLE.SQZ and sent back to the menu,
    #     so we must raise BEFORE yielding the carte / loading — the flow driver catches this and drives the wall. ---
    if is_expert_eater_wall(state):
        raise Pre2ExpertEater()
    # --- 9520 CARTE: the world-map scroll-in the real flow shows between the mode-select and the loader (main 0x01A8).
    #     Loads MAP.SQZ (the map master), stamps the per-level 'you are here' marker (the player's position) onto it =
    #     the de-planarize [0x667a]:[0x62da] (byte-exact vs the VM master), then scrolls the map in via the recovered +
    #     byte-exact build_carte_page until fire, then hands to the loader. ---
    yield from _native_carte(state, dos, game_root)

    # --- the map selected a level ([0x2d8a]); the loader (main 01A5..01D2). native_level_start is VERIFIED byte-exact
    #     vs the pure-ASM oracle's gameplay-entry seed (every core gameplay table identical; see [[pre2-level-init-island]]). ---
    from pre2.native.audio import native_level_song_name, native_load_song
    from pre2.native.level_init import native_level_start
    native_load_song(state, native_level_song_name(state), game_root)   # [asm 01B7] the level song (plays at start)
    native_level_start(state, game_root=game_root)            # [asm 013e..0155] load + init + level-start (lives, etc.)
    state.data[_DS + KEY_TABLE:_DS + KEY_TABLE + 0x80] = bytes(0x80)  # clear the residual key table (the DC1 raw keys)



_ZERO_SINE = bytes(0x100)          # the password/mode-select map does not bounce (row stays 0), so the sine is unused

_PW_CHAR_TABLE = 0xB068     # DS:[0xB068 + scancode] = the ASCII char for a make code [asm 99DD]
_PW_WRONG_FRAMES = 0x8C     # [asm 9AB8] the pause length before the buffer clears + input resumes


def _password_init(state) -> None:
    """Reset the ENTER-CODE screen on entry: empty '[[[[' buffer, no chars/scancode, AND clear the rolling
    cheat-group history + the wrong-code state so a fresh session starts clean."""
    pw = PasswordScreenView(state)
    PlayerGlobals(state).pending_key = 0                     # the last-typed scancode latch (shared with DC1)
    pw.echo[0:4] = b"[[[["                                   # [asm 9ACB] the empty-slot placeholders (0x5B)
    pw.cursor = 0; pw.code = 0; pw.wrong_timer = 0
    pw.hist0 = 0; pw.hist1 = 0; pw.hist2 = 0
    pw.status = 0


def _password_step(state):
    """[asm 9985/99AA-9ADF] One frame of the ENTER-CODE state machine. The runner writes the last-typed hex
    scancode into ``[0x2874]``; this maps it to a hex char (``[0xB068]``), shifts the nibble into the current
    16-bit group ``[0xB1B9]``, and echoes it into ``[0xB170]``. On each completed 4-hex group it checks, in order:

      1. the ``DEAD C0DE F00D <level>`` LEVEL-WARP CHEAT — [asm 9A27-9A64] hash the three PRIOR groups
         (``[0xB1B3/B5/B7]``); on the magic hash the current group is the target level number (``-1`` = the
         ``[0x2D8A]`` index, ``+0x0A`` = expert);
      2. the normal per-level password (``932F`` == :func:`validate_code`).

    A match commits ``[0x2D8A]``/``[0xB197]`` and returns ``(level, expert)``. A miss SHIFTS the group into the
    history (``[0xB1B5]->[0xB1B3]`` etc. — so the cheat sequence can accumulate) and enters a ``0x8C``-frame
    wrong-code pause (``[0xB1AA]``=1) before the echo clears and input resumes. Returns ``None`` otherwise."""
    from pre2.recovered.password import DEFAULT_SEED, is_cheat_sequence, validate_code, warp_index_to_level
    pw = PasswordScreenView(state)
    g = PlayerGlobals(state)
    char_table = ByteTable(state.rb, _PW_CHAR_TABLE)

    if pw.status == 1:                                       # [asm 99AA/9AB4] the wrong-code feedback pause
        pw.wrong_timer = (pw.wrong_timer + 1) & 0xFFFF
        if pw.wrong_timer >= _PW_WRONG_FRAMES:               # [asm 9AC0] pause elapsed -> clear echo, accept again
            pw.echo[0:4] = b"[[[["                            # [asm 9AC8]
            pw.status = 0; pw.cursor = 0                     # [asm 9AD4/9AD9]
        return None                                         # input ignored while the wrong code is shown

    scan = g.pending_key                                     # [asm 99BE] the pending make code
    if not scan or (scan & 0x80):                           # [asm 99C2] none / a break code -> nothing
        return None
    g.pending_key = 0                                        # [asm 99D6] consume it
    al = char_table[scan]                                    # [asm 99DD] scancode -> ASCII
    if al == 0x2D:                                          # [asm 99E1] '-' (a non-code key, e.g. space) -> ignore
        return None
    ah = (al - 0x30) & 0xFF                                 # [asm 99EA] char - '0'
    if ah > 9:                                              # [asm 99ED/99F2] 'A'..'F' -> extra -7
        ah = (ah - 7) & 0xFF
    pw.code = ((pw.code << 4) | (ah & 0x0F)) & 0xFFFF        # [asm 99F5-9A0D] shift the nibble in
    cur = pw.cursor                                          # [asm 9A11] echo cursor / char count
    pw.echo[cur] = al                                        # [asm 9A15] echo the char (the screen re-renders it)
    pw.cursor = (cur + 1) & 0xFFFF                           # [asm 9A19]
    if (cur + 1) < 4:                                       # [asm 9A1F] need 4 chars before validating
        return None

    pw.wrong_timer = 0                                       # [asm 9A28] reset the wrong-code timer for this group
    di, si, bp = pw.hist0, pw.hist1, pw.hist2                # [asm 9A2E-9A36] the three prior groups
    code = pw.code
    seed = pw.bios_seed or DEFAULT_SEED                       # [asm 932F] the BIOS seed (0 on the GOG build -> 0x20)
    rot = state.data[(0x1030 << 4) + 5]                     # cs:[5] rotate count (== 3 on this build)

    m = None
    if is_cheat_sequence(di, si, bp):                       # [asm 9A53-9A62] DEAD C0DE F00D held in the history
        idx = (code - 1) & 0xFFFF                           # [asm 9A64-9A68] the current group is the level number
        if idx <= 0x13:                                    # [asm 9A69] a valid warp target
            m = warp_index_to_level(idx)                    # [asm 9A9B-9AAA] -> ([0x2D8A], expert)
    if m is None:                                           # [asm 9A6E-9A7F] else the normal per-level codes
        m = validate_code(code, seed, rot)

    if m is not None:                                      # [asm 9A9B-9AAA] a level matched -> commit + advance
        level, expert = m
        g.level = level                                    # [asm 9AA6] the selected level
        g.mode = 1 if expert else 0                        # [asm 9AAA] beginner / expert
        return m

    pw.hist0 = si                                           # [asm 9A81-9A94] miss -> shift this group into the
    pw.hist1 = bp                                           #   history ([0xB1B5]->[0xB1B3], [0xB1B7]->[0xB1B5],
    pw.hist2 = code                                         #   [0xB1B9]->[0xB1B7]) so a cheat sequence can build
    pw.status = 1                                            # [asm 9A94] -> the wrong-code feedback pause
    return None


def _native_menu_map(state, dos, game_root: str, kind: str):
    """[asm 96D5] Drive the 0Dh scrolling world-map screen VM-less (``kind`` = ``"password"`` 9985 / ``"mode_select"``
    991F). Yields :class:`FrontEndScene` (MODE_PLANAR) frames — the caveman-tiled map (MOTIF.SQZ) scrolling left, with
    the screen's text (``ENTER CODE`` + the code, or ``MODE`` + BEGINNER/EXPERT) stamped on top each frame via
    ``draw_string`` against the bit-rotated font. Verified pixel-exact vs the VM (all planes, every frame). Raises
    ``Pre2HybridGap`` on the exit — the carte + level-init handoff (the path to gameplay) is the next island."""
    from pre2.codecs.sqz import unpack_sqz
    from pre2.native.assets import resolve_game_path
    from pre2.native.render import native_load_dac_palette
    g = PlayerGlobals(state)                               # named DGROUP access (mode/level commits)
    from pre2.recovered.menu_scene import MenuScenePage, build_shifted_font
    from pre2.recovered.present import scroll_shift_frame
    from pre2.recovered.text import draw_string
    from pre2.recovered.world_map import (MapCamera, map_camera_update, map_page_flip, map_pan,
                                          mode_select_input, mode_select_text_runs, password_text_runs)
    rb, rw = readers(state)
    native_load_dac_palette(state, dos, 0xB118, 0x10)             # [asm 97A5] the map's 16-colour palette
    page = MenuScenePage()
    motif = unpack_sqz(resolve_game_path(game_root, "MOTIF.SQZ").read_bytes())[:0x3E80]
    page.seed(motif)                                              # [asm 96EC/9718] planes 0,1
    fseg = LoaderGlobals(state).font_seg                                 # the font segment ([0x3d])
    font = build_shifted_font(bytes(state.data[(fseg << 4):(fseg << 4) + 0x3000]))          # [asm 972E] shift copies

    def text_runs():
        return password_text_runs() if kind == "password" else mode_select_text_runs(g.mode)

    def cstr(off):                                               # the NUL-terminated string at DS:off
        end = state.data.index(0, _DS + (off & 0xFFFF))
        return bytes(state.data[_DS + (off & 0xFFFF):end])

    cam = MapCamera(0, 0, 2, 0, 0, 0)                            # [asm 978D] phase seeds at 2
    prev_page = 0
    prev_arrow = False                                          # edge-detect the arrow for the BEGINNER/EXPERT toggle
    bounce = state.data[(0x1030 << 4) + 5] != 0                # [asm 9AF5] cs:[5] (=3): the vertical sine-bounce is ON
    sine = bytes(state.data[_DS + ROW_SINE_TABLE:_DS + ROW_SINE_TABLE + 0x100])  # [asm 9B00] the row-bounce sine table (DGROUP 0x6f90)
    pal = tuple(dos.vga_palette)

    scr = "mode_select" if kind == "mode_select" else ""           # the ONLY screen the touch host reads swipes on

    def scene(planes, page_off, pel):
        return FrontEndScene(MODE_PLANAR, palette=pal, planes=tuple(bytes(p) for p in planes),
                             page=page_off, pel=pel, wrap=0x1FFF, screen=scr,
                             enh=("menu", motif, cam.x, cam.row))   # bg pattern + scroll phase (see FrontEndScene)

    matched = None                                              # (password) a valid ENTER-CODE accepted -> its level
    if kind == "password":
        _password_init(state)                                  # start with the empty '[[[[' buffer, no stale key
    yield scene(page.planes, 0, 0)                               # frame 0 = the seeded page
    while True:
        cam = map_camera_update(cam, bounce=bounce, sine_table=sine)   # [asm 9AE0] scroll left 4/frame + row sine-bounce
        ds, pel = map_pan(cam.x, cam.row)                        # [asm 97A8/97F2] display-start AND pel from the
        #   SAME (current) x = [0xB19D]&7 — they MUST be consistent. (An earlier build took pel from prev_x, which
        #   mismatched ds's byte-column and made the whole screen jump +/-8 px each frame — the "shaking".)
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
            arrow_sc = (0x48 if g.in_up else 0x50 if g.in_down else       # up / down
                        0x4D if g.in_right else 0x4B if g.in_left else 0)    # right / left
            if arrow_sc and not prev_arrow and mode_select_input(arrow_sc, False).toggle:
                g.mode ^= 1                                    # flip the selection (the text re-renders next frame)
            prev_arrow = bool(arrow_sc)
        elif kind == "password":                               # [asm 9985] accumulate the typed hex code
            m = _password_step(state)
            if m is not None:                                  # a VALID 4-char code AUTO-ADVANCES (no separate
                matched = m                                    # confirm) — [0x2D8A]/[0xB197] hold the matched level.
                g.mode_copy = g.mode
                g.attract_mode = g.mode
                yield from _planar_fade_out(state, 0xB118, page.planes, ds, pel, enh=("menu", motif, cam.x, cam.row))   # [asm 9286] fade to black
                return                                          # ...then the carte + loader for the chosen level
        if (g.in_fire | g.fire_alt) != 0:                        # fire = confirm / leave
            if kind == "mode_select":                          # [asm 8F12/8F18/8ED7] commit the difficulty, start L1
                g.mode_copy = g.mode
                g.attract_mode = g.mode
                g.level = 0                                     # BEGINNER/EXPERT both begin at level 1
                yield from _planar_fade_out(state, 0xB118, page.planes, ds, pel, enh=("menu", motif, cam.x, cam.row))   # [asm 9286] fade to black
                return                                          # ...then the carte (its own palette) scrolls in
            # password: FIRE with no valid code EXITS the screen too — [0x2D8A] stays 0xFF (set at entry, 8EE9)
            # and the 8E45 dispatch loops back to the press-1/2 menu ([asm 8EF1-8EF8] jge/jmp — the original
            # "give up on the code" escape; a valid 4-char code instead auto-advanced above with its level).
            yield from _planar_fade_out(state, 0xB118, page.planes, ds, pel, enh=("menu", motif, cam.x, cam.row))       # [asm 9286] fade to black
            return


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
    g = PlayerGlobals(state)
    saved_top = LoaderGlobals(state).load_top                      # [asm 9530] MAP.SQZ loads at the top...
    seg = load_sqz(state, "MAP.SQZ", game_root=game_root)
    LoaderGlobals(state).load_top = saved_top                    # ...but is transient — restore the load pointer so
    #                                                                the loader stacks the level exactly where the VM does
    master = bytes(state.data[(seg << 4):(seg << 4) + 0xFA00])   # the 4-plane map master (planes @0/3E80/7D00/BB80)
    # [asm 9543-95CD] stamp the per-level 'you are here' marker (the player's caveman on the map) into the master.
    lg = LoaderGlobals(state)
    lv = g.level                                              # the level index picks its map (x,y) + the marker
    dims = lg.carte_marker_dims; mw = (dims & 0xFF) >> 3; mh = dims >> 8   # marker size (bytes wide / rows) [asm 9562-956A]
    di = carte_marker_offset(rw(CARTE_MARKER_TABLE + lv * 4), rw(CARTE_MARKER_TABLE + 2 + lv * 4))
    msrc = (lg.carte_mask_seg << 4) + lg.carte_mask_off                        # [0x667a]:[0x62da] — mask + 4 colour planes
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
        # [asm CRTC r01 -> 312px] the carte narrows the H-display to 312 (39 chars) while it pel-pans, hiding the
        # right 8 columns where the not-yet-revealed / wrap seam would otherwise show. Match it so no artifact leaks.
        yield FrontEndScene(MODE_PLANAR, palette=pal, planes=tuple(bytes(p) for p in planes),
                            page=ds, pel=pel, wrap=0x1FFF, active_width=312,
                            enh=("carte", master, scroll_x))   # 640px stamped master + reveal (see FrontEndScene)
        apply_ds(state, decode_input(rb, rw))
        fire = (g.in_fire | g.fire_alt) != 0
        # confirm on a fresh fire press OR when the scroll-in reaches the end (auto-advance) — either way the
        # carte fades to black before the level loads (the VM does not snap from the map straight into gameplay).
        if (fire and not prev_fire) or scroll_x >= 639:
            yield from _planar_fade_out(state, 0xB0E8, planes, ds, pel, enh=("carte", master, scroll_x))   # [asm 9286] fade to black -> the loader
            return
        prev_fire = fire
        if scroll_x < 639:                                     # scroll the map in +1/frame (the VM's [0xb19d] rate)
            scroll_x += 1


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


def native_expert_eater(state, game_root: str):
    """[asm 0163->0178] The "YOU MUST BE AN EXPERT EATER" wall.

    When a BEGINNER ([0xB197]==0) advances into level 8 or 9 ([0x2D8A] in {8,9}), main's 0x0163 gate shows
    CASTLE.SQZ (resource 0x2C, ``dx=0x2c`` -> 107B) INSTEAD of loading the level: fade it IN (919F, ``cl=0xFF``
    = the full 256-entry DAC), HOLD it while the scene-wait (0BBE) waits for the fire key press+release, then
    re-enter the press-1/2 MENU (0199 ``mov ax,1``; jmp 012F -> 8E45). There is NO fade-OUT (unlike THE END).

    A GENERATOR yielding :class:`FrontEndScene` frames; the caller re-enters ``native_menu_flow`` when it returns.
    Same 13h-image + fade + 0BBE-wait primitives as :func:`native_the_end`, reused rather than re-derived."""
    image = render_image_scene("CASTLE.SQZ", game_root)                # [asm 0178->107B res 0x2c] 64000-byte 13h image
    pal6 = image_palette("CASTLE.SQZ", game_root)                      # the 256-entry DAC at the asset's head
    fade_in = fade_in_frames(pal6, 0xFF)                              # [asm 018C cl=0xFF -> 919F] black -> target
    held = fade_in[-1]
    for p6 in fade_in:                                                 # [asm 919F] fade-in retrace frames
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=image)
    held8 = _expand_palette6(held)
    phase = WAIT_PRESS                                                 # [asm 0196 -> 0BBE] wait for fire press+release
    while True:
        yield FrontEndScene(MODE_LINEAR, palette=held8, linear=image)
        phase, done = native_scene_wait(state, phase)
        if done:
            break
    # [asm 0199 mov ax,1 / jmp 012F -> 8E45] no fade-out — the caller re-enters the press-1/2 menu directly.


def is_expert_eater_wall(state) -> bool:
    """[asm 0163-0176] True when a BEGINNER (``[0xB197]==0``) has advanced to level 8 or 9 (``[0x2D8A]`` in
    {8, 9}) — where main's 0x0163 gate shows the CASTLE "expert eater" wall (:func:`native_expert_eater`)
    instead of loading the level. Beginner can only PLAY levels 0..7; 8 (penguin) is the expert-only wall."""
    g = PlayerGlobals(state)
    return g.level in (8, 9) and g.mode == 0


def native_the_end(state, game_root: str):
    """[asm 5034] THE END — the game-complete screen (the player cleared the final level 0xE, [0x6be5]==0xFF).

    Loads ``THEEND.SQZ`` (a 16-colour 13h image, ``dx=0x6ba0`` -> 107B) and shows it the way 5034 does: fade it
    IN (919F, ``cl=0x10`` = 16 DAC entries), HOLD it while the scene-wait (0BBE) waits for the fire key to be
    pressed AND released, then fade it OUT (9286). A GENERATOR yielding :class:`FrontEndScene` frames so the flow
    driver presents the whole sequence; when it returns the caller re-enters the front-end (attract/title), which
    is where 5034's carry (-> main 0x12f) lands. FINAL.TRK (the ending song) is loaded by the caller (audio seam).

    The 16-colour asset + 0x10 fade + 0BBE wait are the same primitives as ``_native_title_screen`` (TITUS) and
    ``native_scene_wait`` (the OLDIES wait), reused rather than re-derived."""
    image = render_image_scene("THEEND.SQZ", game_root)                # [asm 5052->919F] 64000-byte 13h image
    pal6 = image_palette("THEEND.SQZ", game_root)                      # [asm 5052] DS:si palette (16 live entries)
    fade_in = fade_in_frames(pal6, 0x10)                               # [asm 919F cl=0x10] black -> target
    held = fade_in[-1]
    for p6 in fade_in:                                                 # [asm 919F] fade-in retrace frames
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=image)
    held8 = _expand_palette6(held)
    phase = WAIT_PRESS                                                 # [asm 5055 -> 0BBE] wait for fire press+release
    while True:
        yield FrontEndScene(MODE_LINEAR, palette=held8, linear=image)
        phase, done = native_scene_wait(state, phase)
        if done:
            break
    for p6 in fade_out_frames(held, 0x10):                             # [asm 5058 -> 9286] fade-out retrace frames
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=image)
    # [asm 505B] the CREATORS screen (year-gated Easter egg) follows the THEEND fade-out, in the same 5034 call.
    yield from native_creators_screen(state, game_root)


# --- the CREATORS "HOLLY DAY IN MEYRUES!" photo screen (25F6) — mode 12h (640x480 planar, grayscale) ---
MODE_CREATORS = 0x12       # VGA 640x480x16 planar, one-off (the only 12h screen); grayscale DAC ramp
_CREATORS_W, _CREATORS_H = 640, 480
_CREATORS_PLANE = 0x81B0   # [asm 2671 cx=0x40d8 words] bytes per plane blitted (80 * 415 rows)
_CREATORS_ROWBYTES = 80


def native_creators_screen(state, game_root: str):
    """[asm 25F6] The developers' "HOLLY DAY IN MEYRUES!" photo montage shown after THE END (call 505B), gated
    on the system year ``[0x37] >= 0x7CA`` (1994) — an Easter egg that is always on now.

    A mode-12h (640x480) 4-plane GRAYSCALE screen: LEVELH.SQZ supplies planes 0,1 (blit 2652-2681, map-mask
    0x01/0x02) and LEVELI.SQZ planes 2,3 (blit 268D-26BC, map-mask 0x04/0x08); the DAC is a 16-step grey ramp
    ([asm 2636-264B] 0,4,8,..,0x3C). A GENERATOR yielding one :class:`FrontEndScene` (MODE_CREATORS) per frame
    while the scene-wait (0BBE) holds for the fire key; when it returns the caller continues to the menu.

    Native decodes the two SQZ assets directly (the VM's 12h planar blit needs no CRTC emulation) and hands the
    four 640x480 bit-planes + the grey palette to the runner, which deplanarizes them (like the 0Dh screens but
    at 640-wide). No DGROUP writes — a pure render scene, so it needs no oracle."""
    if LoaderGlobals(state).year < 0x7CA:                                   # [asm 25F6] year < 1994 -> skip
        return
    from pre2.codecs.sqz import unpack_sqz
    import os
    with open(os.path.join(game_root, "LEVELH.SQZ"), "rb") as f:
        h = unpack_sqz(f.read())
    with open(os.path.join(game_root, "LEVELI.SQZ"), "rb") as f:
        i = unpack_sqz(f.read())
    P = _CREATORS_PLANE
    planes = (bytes(h[0:P]), bytes(h[P:2 * P]), bytes(i[0:P]), bytes(i[P:2 * P]))   # planes 0,1,2,3
    grey = tuple((_dac8(v * 4), _dac8(v * 4), _dac8(v * 4)) for v in range(16)) + ((0, 0, 0),) * 240
    phase = WAIT_PRESS                                                     # [asm 26BE -> 0BBE] wait for fire
    while True:
        yield FrontEndScene(MODE_CREATORS, palette=grey, planes=planes)
        phase, done = native_scene_wait(state, phase)
        if done:
            break


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
