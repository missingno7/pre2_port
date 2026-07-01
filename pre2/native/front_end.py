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

    # --- PRESENT.SQZ title (9090): 02cc loads the menu song (an audio command, not a visual frame), then the
    #     "PREHISTORIK 2" title — fade-IN 256 over the background, then the 911D palette MORPH 234 over the
    #     background+logo (the title's colour reveal). The morph target is the static palette at DGROUP 0xACE7. ---
    morph_target = bytes(b & 0x3F for b in state.data[_DS + 0xACE7:_DS + 0xACE7 + 0x300])
    yield from _native_present_screen(game_root, morph_target)

    # --- 2dfa: decode the shared SPRITES.SQZ sprite bank into the game state (the title is still on screen — no new
    #     visible frame). This is the bank the world-map + gameplay need. Verified byte-exact (native_build_sprite_bank,
    #     [[pre2-front-end-flow]]) and composes from the post-title state ([0x2875] 0x27be -> 0x57a8). ---
    from pre2.native.sprite_bank import native_build_sprite_bank
    native_build_sprite_bank(state, game_root=game_root)

    # --- next: the world-map level-select. The flow is the 8e45 dispatch (the 13h "press 1/2" mode-select prompt) ->
    #     96d5 the 0Dh scrolling world-map (mode-select 991f / password 9985 callback) -> 965a carte -> level-init ->
    #     gameplay. Every leaf is recovered (recovered/world_map.py); composing + verifying each scene vs the VM (the
    #     same A000×DAC diff method that proved OLDIES pixel-exact) is the next island. ---
    raise Pre2HybridGap(
        "native front-end: OLDIES + TITUS + PRESENT titles + the 2dfa sprite-bank decode driven VM-less (OLDIES "
        "pixel-exact vs the VM). Next: the world-map level-select (8e45 'press 1/2' -> 96d5 scrolling map -> carte) (#14).")


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
    for p6 in palette_morph_frames(held, morph_target, initial_phase=phase):     # [asm 911D] colour reveal
        yield FrontEndScene(MODE_LINEAR, palette=_expand_palette6(p6), linear=full)
