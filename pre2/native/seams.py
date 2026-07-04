"""The VM↔native seam constants — the main-loop capture sites + the gameplay-vs-render DGROUP ownership map.

Moved VERBATIM from ``pre2/probes/probe_native_frame.py`` + ``probe_native_forward.py`` (which re-export them):
the NATIVE layer needs these (``game_tick_demo``'s digest is defined by ``_FWD_EXCL``), and native must not import
the probes (they import the VM — ``pre2.runtime``/``play`` — at top level, which dragged the whole emulator into
the standalone's import closure). Pure: no ``cpu``/``mem``/``dos_re`` imports.

Two families:
* the SEAM SITES (``FRAME_TOP``/``DECODE``/``KEY_SAMPLE``/``GAP_SITE`` + ``KBD``) — where a VM oracle snapshots
  the seed, samples the keys, and reads the post-tick state;
* the OWNERSHIP EXCLUSIONS (``_EXCL``/``_FWD_EXCL`` + parts) — the DGROUP offsets the gameplay step does NOT own
  (render/audio/input-plumbing state), i.e. the byte-compare mask under which "same gameplay" is defined.
"""
from __future__ import annotations

from pre2.native.player import RENDER_OFFSETS
from pre2.recovered.input_decode import _KBD_SOURCES

DS = 0x1A0F
DS_BASE = (DS << 4) & 0xFFFFF
FRAME_TOP = 0x021A          # main-loop top: save the native seed here
DECODE = 0x0DC1             # DC1 ENTRY: the input decoder starts here (mode dispatch)
KEY_SAMPLE = 0x0F0A         # DC1 AFTER the [0x28xx] scancode reads (0EA4-0F06): capture the keys the VM actually
#                             sampled HERE, not at the 0DC1 entry — INT 09 can set a key BETWEEN entry and the
#                             reads, so a 0DC1 capture misses it and a forward-carry oracle then diverges by one
#                             key for that frame (player X/vel + input flags), a pure capture-timing artifact.
GAP_SITE = 0x0270           # the loop-back (jmp 0x214): native_gameplay_frame now runs the WHOLE loop 021A..026D.
#                             The render calls (3668/35A1/3A27/4B8E/26FA/3721/6772) are the renderer's job and are
#                             not run here; their render-state DGROUP writes are excluded below so this stays a
#                             gameplay-state cmp. Death/level-change frames (4C69 carry -> 0x12f) never reach 0x0270.
KBD = tuple(sorted({o for srcs in _KBD_SOURCES.values() for o in srcs}))   # the 21 keyboard flags DC1 reads

# async audio/ISR scratch (PIT/SB) + render state the gameplay step doesn't own:
#  - 454E player-sprite bg-save slot (RENDER_OFFSETS)
#  - 8922 object_particles (a render island skipped here) builds the effect draw-list: the render slots
#    [0x52E8..0x5450) (20 x stride 0x12) + the effect-sprite source list [0x8F1D..0x9107) (bounce animation).
_RENDER_DRAWLIST = set(range(0x52E8, 0x5450)) | set(range(0x8F1D, 0x9107))
# more render state the gameplay step doesn't own (the renderer's job, run by native_render):
#  - the page-flip buffer segs [0x2dd6]/[0x2dd8] (front/back VRAM page, swapped each frame)
#  - 3668's redraw counters [0x6bd4] + [0x6bc3] (the 0x66->0x68 dither rotation) + the ISR timer tick [0x27ee]
_PAGE_FLIP = {0x2DD6, 0x2DD7, 0x2DD8, 0x2DD9}
_RENDER_COUNTERS = {0x27EE, 0x27EF, 0x6BD4, 0x6BC3}
# 35A1 (dirty-grid redraw) + 3A27 (scroll-copy) own the smooth HORIZONTAL-scroll render state (5643/camera_follow
# does only the vertical): the displayed scroll-X counter [0x2de0], the scroll-copy SOURCE pointer [0x2dba/bb]
# (calc_scroll_source; render_frame's `scroll_src` ring-buffer offset — the renderer 3A27/348D owns it, the
# gameplay step's camera-follow only writes it as a by-product), and the dirty-grid / background-pointer block
# [0x2dee..0x2df8) (incl. [0x2df6] bg ptr) — render, run by native_render, not the gameplay step.
_SCROLL_RENDER = {0x2DBA, 0x2DBB, 0x2DE0, 0x2DE1} | set(range(0x2DEE, 0x2DF8))
# each object slot's +5 byte holds render page/visibility flags (bits 0x20|0x40) that 26FA (object_render) writes
# -> mask just those two bits so the gameplay low bits still verify (slots: base 0x4F0A, stride 0x12).
_SLOT5_PAGE = {0x4F0A + i * 0x12 + 5 for i in range(0x75)}
# each rendered sprite's +0x11 byte is the object_render attr/animation countdown (the recovered 0x1FFF attr):
# 60FE (particles) seeds it, then 26FA (object_render) decrements it each frame as it draws — render-owned
# (native_render decrements it), so the gameplay step leaves it for the renderer (base 0x4F0A, stride 0x12).
_SLOT_ATTR = {0x4F0A + i * 0x12 + 0x11 for i in range(0x75)}
# 1C65 (the vsync/frame-sync wait inside 44FB) maintains the page-flip-pending counter [0x6be7] — timing/waiting
# machinery the native port replaces with its heartbeat (lifecycle Phase 10), never gameplay state.
_TIMING = {0x6BE7}
# 4624's player-sprite render slot [0x6CA0..0x6CA2): a sibling of 454E's bg-save slot [0x6CA2..0x6CA6] — the
# saved sprite position the renderer compares + writes (1030:460C cmp [0x6CA0] / 4624 mov [0x6CA0],dx). The
# gameplay step never writes it (native_gameplay_frame leaves it at the seed; native_render owns it), so it is
# render state, not gameplay. (Surfaced by the game-tick verifier at gorilla tick 585 as a 2-byte stale.)
_PLAYER_REDRAW = {0x6CA0, 0x6CA1}
# [0x6BBD] is the third combat_interaction REDRAW_DIRTY flag (with [0x2DF4]/[0x2DE0], already excluded above):
# set when a consumed tile needs redraw, then read + cleared by the render (3668/animation). native_render owns
# the clear, so the gameplay step leaves it set -> render-dirty signal, not gameplay state. (The consumed tile's
# actual map/score change is verified elsewhere.) Surfaced by the game-tick verifier at gorilla tick 611.
_REDRAW_DIRTY = {0x6BBD}
# the SFX / sound-engine working block. play_sfx (0282) writes the descriptor [0x1004-0x1007] (already above);
# the sound engine then owns [0x1035..0x1220): the 11 PC-speaker note structs [0x1035..0x10A5] AND the digital
# SoundBlaster channel/mix + DMA-block state [0x10A5..0x1218] the SB ISR churns every frame while a sound plays
# (observed extent over a full level demo; the block size is fixed by the game's SB driver). This is AUDIO, not
# gameplay: the VM-less core has no SB (its audio is a separate native system), so it never writes this region.
# It MUST be excluded or a demo replayed WITH the SB (as recorded) diverges here every tick even though the
# gameplay is byte-identical — the trap that made a desynced tick-demo look like it "passed".
_SFX_ENGINE = set(range(0x1035, 0x1220))
_EXCL = (set((0x1004, 0x1005, 0x1006, 0x1007)) | _SFX_ENGINE | set(range(0x27F0, 0x2800))
         | set(range(0x2820, 0x2880)) | set(range(0xAB0, 0xE00)) | set(RENDER_OFFSETS) | _RENDER_DRAWLIST
         | _PAGE_FLIP | _RENDER_COUNTERS | _SLOT_ATTR | _SCROLL_RENDER | _TIMING | _PLAYER_REDRAW | _REDRAW_DIRTY)

# --- the forward-oracle additions (probe_native_forward) — the standalone-forward compare's extra ownership --- #
# the demo-RECORD/playback RLE buffer: input plumbing the standalone runner never uses (it reads live input /
# injects per-tick keys). The record encoder (0F0A-0F70) writes {value, count} pairs at [si+0x3d] as the game
# plays, `si = [0x287a]` (the write cursor) advancing by 2 up to the 0x7FC cap (0F0E) — so the WHOLE buffer
# [0x3d .. 0x83c) is progressively OVERWRITTEN by the recorder and cannot hold persistent gameplay state. The VM
# fills it while replaying; native (no record) leaves it at the seed's 0x55AA. (Earlier verifies used SHORT demos
# whose RLE only reached ~0x60; a 1579-tick finish demo fills far more, so size the exclusion to the real buffer.)
_DEMO_RLE = set(range(0x3C, 0x83C))
# the render slot array (base 0x4F0A, stride 0x12): slot 1 is the player, slots >=2 are render records.
# native NOW maintains these (8922 project_particles + the 26FA record-mutation half are wired into
# native_gameplay_frame), so with this exclusion the WHOLE gorilla demo runs forward clean = GAMEPLAY reproduced.
# A pure-RENDER residual still diverges ~frame 141 without the exclusion (a remaining render producer: 4B8E
# particles_draw / the terrain 0x5570 compaction) — it never cascades into gameplay (the 318-frame run proves it),
# so it is excluded here to keep this the clean gameplay-completeness metric. Slot 1 + all entity DATA stay checked.
_SLOT_BASE, _SLOT_STRIDE = 0x4F0A, 0x12
_NSLOTS = max((p - (_SLOT_BASE + 5)) // _SLOT_STRIDE for p in _SLOT5_PAGE) + 1
_RENDER_SLOTS = {_SLOT_BASE + k * _SLOT_STRIDE + f for k in range(2, _NSLOTS) for f in (4, 5, 0xC, 0xD)}
# the HUD (45B8, render — native skips it) FORMATS its DGROUP layout buffers each frame: the 6-digit score string
# [0x6F52], the BONUS-letter table [0x6F86], etc. native never writes them, so they go stale in the forward run
# (the score VALUE is gameplay + stays checked; only the formatted display band is excluded).
_HUD = set(range(0x6F4E, 0x6FA0))
# the previous-camera cells [0x2DE0]/[0x2DE2] are render scroll state (the runner's native_sync_render_state +
# native_render maintain them; native_gameplay_frame does not). _EXCL already covers the horizontal [0x2DE0];
# add the vertical [0x2DE2] for levels that scroll vertically (the gorilla demo doesn't, so it was never needed).
_SCROLL_Y = {0x2DE2, 0x2DE3}
# the object-render (26FA) blit's per-sprite clipped-extent scratch word ([0x2DEC/D], written 27DE/27E1; the only
# reader is the blit itself — the respawn probes already exclude it). Render-only, so exclude from the gameplay verify.
_BLIT_SCRATCH = {0x2DEC, 0x2DED}
# [0xA32E] PROJ_SLOT_PTR — the object projection's "last projected slot" scratch (object_inject 7DF0/697D): it
# points at the object/render slot the projection writes the anim descriptor into. The projected slot CONTENTS
# match the VM byte-exact; only which free slot the pool handed out differs (render-pool allocation order — the
# render-record gap that "never cascades to gameplay", proven on gorilla). Render-record scratch -> exclude, like
# the render slots. (demo 175517: after the death-bounce particle-consume fix this was the sole residual @tick1299.)
_PROJ_PTR = {0xA32E, 0xA32F}
# The LEVEL-TRANSITION render tables — produced by the VM's iris-wipe (3239) + sprite-classify (39DF) during a
# level change, then consumed by the renderer. They are STABLE + byte-exact WITHIN a level (so they never diverged
# in single-level verification), but at a level BOUNDARY the VM rebuilds them and the state-only native transition
# (native_level_end, no iris/reveal render) doesn't — a pure RENDER divergence (the pixel-exact render probes verify
# them, not the gameplay digest). Excluding them is the same gameplay/render boundary the rest of _FWD_EXCL draws:
#  - [0x2DDA] level-data base seg + [0x2DDC] shared sprite-bank ptr: ASSET-ADDRESSING (WHERE the level data + bank
#    are loaded — the load pointer [0x2875] differs by the VM's transient carte/MAP.SQZ load). The data CONTENT the
#    pointers reach is verified transitively (gameplay collision/render reads it correctly), and it lives above the
#    64KB DGROUP so it is not in the digest anyway — only the base pointer value differs.
#  - [0x2DF8..0x4DF8) transparency masks (256 x 0x20): the partial-sprite blit masks (native_level_load_classify).
#  - [0x2DC0..0x2DD4) the iris-wipe centre/clamp/counter SCRATCH (transition.py: X_CLAMP/X_OFF/Y_OFF/CUR_Y/RUNNING/
#    COLCNT/RADIUS/CUR_X) — set by the 316F/3054 iris close/open which the headless transition skips.
#  - [0x6A88..) TBL_Y (0x41 words) + [0x6B14..) TBL_X (0x41 words): the iris-wipe scaled-column geometry (3227/3239).
#  - [0x6CA9..0x6EA9) the 0x100-word RANDOM decor table native_5237 fills from the CARRIED rng (5274, BEFORE the
#    level re-seed) — non-deterministic by design (the VM's tally advances the rng differently than native_exit_anim;
#    the DETERMINISTIC gameplay rng [0x2CEC] is re-seeded right after and stays verified). Cosmetic, no gameplay read.
#  - [0xB19D..0xB1A5) the world-MAP / carte camera (fine-X / row / page-draw / page-clear) — the between-levels carte
#    scene the headless transition skips; only the map render reads it.
#  (the [0x0550..] bytes that first looked like a load table are actually the demo-RECORD RLE buffer — see _DEMO_RLE.)
_XITION_RENDER = ({0x2DDA, 0x2DDB, 0x2DDC, 0x2DDD} | set(range(0x2DF8, 0x4DF8)) | set(range(0x2DC0, 0x2DD4))
                  | set(range(0x6A88, 0x6A88 + 0x41 * 2)) | set(range(0x6B14, 0x6B14 + 0x41 * 2))
                  | set(range(0x6CA9, 0x6EA9)) | set(range(0xB19D, 0xB1A5)))
# The raw INT-09 keyboard cells the six DC1 flags OR together (KBD — the injected-input plumbing). The tick
# recorder captures the CONSUMED flag values at KEY_SAMPLE and synthesizes a flag-equivalent cell image
# (first source cell = flag value), because capturing the raw cells races a mid-window INT 09 against the
# flag reads (safe demo 230900 tick 1858: a release landed between a cell's OR-read and 0F0A). The synthesized
# image reproduces the flags exactly but not WHICH sibling cell held the key — input plumbing, same ownership
# class as the demo-RLE buffer, so exclude the cells themselves from the digest (the CONSUMED flags [0x27E8..
# ED] remain fully checked).
_KBD_CELLS = set(KBD)
_FWD_EXCL = (set(_EXCL) | _DEMO_RLE | _RENDER_SLOTS | _HUD | _SCROLL_Y | _BLIT_SCRATCH | _PROJ_PTR
             | _XITION_RENDER | _KBD_CELLS)
