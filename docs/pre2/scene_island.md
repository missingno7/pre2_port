# Scene island — the non-gameplay visual seam (`SceneState → render_scene`)

The gameplay frame collapsed into a meaningful seam: `RendererState → render_frame(...)`.
The startup / title / menu / map / loading / tally screens need the **same kind of target**
so we recover their *visual intent*, not a pile of isolated VGA hooks:

```
scene logic / state machine        (BORDER — owns "which screen", input, transitions)
   -> SceneState                    (this contract: a plain-data description of the screen)
   -> render_scene(state, target)   (FAITHFUL leaves: image, text, cursor, palette)
   -> RenderTarget                  (faithful VGA: planes 0Dh | linear 13h + DAC)
        |  enhanced: same SceneState -> own RenderTarget (true-colour buffer, no VGA/CRTC/flip)
```

## Two video modes (confirmed by extracting real snapshots)

PRE2 draws scenes in two modes, so `RenderTarget` + `SceneImage` carry both:

* **mode 0Dh** — planar 16-colour (menu / map / score / tally): four EGA bitplanes; text via
  `draw_string` (planes 2|3). `SceneImage.planes`, `RenderTarget.planes`.
* **mode 13h** — linear 256-colour (intro / title artwork): a 320x200 indexed image at
  A000:0000. `SceneImage.pixels`, `RenderTarget.linear`. (Verified: snapshot 163804 is a real
  227-colour full-screen image; 233517/190338 are 16-colour screens.)

`render_scene` dispatches on `state.video_mode`. The text path runs only in 0Dh (`draw_string`
is planar planes 2|3); the 13h path is image + 256-colour palette.

This mirrors gameplay exactly: there, *game logic* produces `RendererState` and `render_frame`
only draws it; the object system (which owns gameplay state) is the border. Here, the *scene
state machine* produces `SceneState` and `render_scene` only draws it; the state machine is the
border.

## What is a "scene"?

A non-gameplay screen is not a scrolling world — it is a **static composition of a few
primitives**, re-presented each frame:

* a **full-screen background image** (a SQZ-decoded picture; the codec is recovered, the
  *present* path is not),
* zero or more **text runs** (strings drawn by `draw_string`),
* a **palette** (16-colour DAC), possibly **fading** in/out,
* an optional **menu cursor / highlight** (the selected item),
* **page / present** bookkeeping (which page is visible; vsync) — a faithful-only quirk.

So `SceneState` is a small display list, not a fixed struct. `render_scene` composes the
leaves in z-order: background → text → cursor, with the palette applied to the DAC.

## `SceneState` contents (the contract)

| Field | Meaning | Source (VM memory / asset) | Leaf |
|---|---|---|---|
| `scene_id` / `phase` | which screen + transition phase (intro/title/menu/map/loading/tally) | scene state machine (border) | — |
| `background: SceneImage` | the four EGA bitplanes of the full-screen picture | SQZ-decoded image asset | `present_image` (provisional) |
| `font: bytes` + `text_runs: [TextRun]` | strings + per-run pen/shade/page | font segment `[0x2875]`, state block `[0xB1A0..]` | `draw_string` (RECOVERED) |
| `palette` / `fade: FadeStep` | static 16-colour DAC or a fade step | `[0xACB7]` target, `[0x2D00..]` src, `9200` install | `fade_palette` (RECOVERED) |
| `cursor: MenuHighlight` | the selected menu item | menu state (border) | `draw_cursor` (TBD) |
| `page_visible` / `page_draw` | double-buffer pages (faithful page flip) | CRTC start-address `9600` | present (faithful only) |

`SceneState` is **plain data** (no `mem`), reconstructed read-only by `pre2/bridge/scene_state.py`
— the single bridge, exactly like `render_state.py` for gameplay.

## Scene logic vs. renderer leaves (the border test)

Same border test as the gameplay island: *a routine is a renderer leaf iff it only reads state
and writes VRAM/DAC, with no scene decision.*

**Renderer leaves (the scene island — to recover):**
* `draw_string` (`1030:9886`) — text → planes 2|3. **RECOVERED** (`pre2/recovered/text.py`).
* `present_image` — full-screen picture → planes. *Provisional* (exact ASM present unrecovered).
* `fade_palette` (`6772`) / palette install (`9200`) — DAC. fade **RECOVERED**; install TBD.
* `draw_cursor` — the menu highlight blit/inversion. **TBD** (needs a menu witness).
* presentation setup `9600` (CRTC start / sequencer) — faithful page-flip plumbing.

**Scene logic (the border — NOT recovered into the renderer):**
* which scene is active and the intro→title→menu→map→gameplay→tally **transitions**,
* **menu navigation**: input → cursor move → selection,
* **transition timing** (when to fade, when to switch scenes),
* asset **loading** orchestration (SQZ load of the image/font; the codec is recovered).

These produce `SceneState`; they are the scene equivalent of the object system. They will be
recovered later as a separate **scene state machine** island (its own state model), not folded
into `render_scene`.

## Faithful vs. enhanced

* **Faithful `render_scene(state, planes, dac)`** reproduces the original pixels + DAC into the
  EGA planes for **verification** against the VM/VGA oracle (lockstep at each leaf's RET, like
  the gameplay leaves). It preserves planar/page/DAC quirks on purpose.
* **Enhanced/native** reimplements `render_scene` against the *same* `SceneState`, drawing to
  its own framebuffer — free of planar VRAM, CRTC, page flips, and the 16-colour DAC. The
  background can be a true-colour image, text can be re-rasterised, fades become alpha — all
  driven by the recovered *intent* in `SceneState`, not by copying VGA bytes.

## Recovery order (each leaf merges UP into `render_scene`, never a standalone hook)

1. **`draw_string` / text** — recovered; **verify** against a mid-draw witness, then wire as
   the `render_scene` text leaf. *(first concrete island — this change starts it.)*
2. **Scene-present setup** — `9600` CRTC/sequencer + `9200` palette install: the page/palette
   plumbing that frames a scene draw. **Pan + page flip done** — `pre2/recovered/present.py`
   (`present_pan_flip` / `compute_display_start`, `1030:9613..9639`): `display_start =
   (scroll_x>>3 + scroll_y·0x28) & 0x1FFF`, `page_draw = display_start`, `page_clear = old
   page_draw`. VERIFIED 321/321 live menu-scroll steps + a golden test (`tests/test_present.py`).
   Note `9600` as a whole is the mode-select **scene controller** (the scroll loop + redraw +
   background shift-copy + hold), i.e. the border — only its present arithmetic is the leaf.
   **Menu background done too** — `scroll_blit_column` (`1030:965A..969C`): every 8 px of pan it
   blits one fresh byte-column of the master pattern (segment `[0x2875]`) into all 4 EGA planes
   (wrapping at the `0x2000` circular page), feeding the infinite scroll the CRTC pan reveals.
   VERIFIED 79 blits / 553 skips / 0 divergence + a regression test. **Wired live too**
   (`pre2/checkpoints/present.py` + `pre2/bridge/present.py`): the `965A..969C` block is the
   map/menu scroll's hottest loop (~474 interpreted instr/call, ~half the scroll frame), so the
   native blit lets the VM keep up with the present rate → the map scroll renders **smoothly**
   (it's vsync-gated, so the scroll *speed* is unchanged — only the per-frame CPU cost drops).
   Per-call whole-state lockstep on the map snapshot: all live memory byte-exact, only clobbered
   scratch regs differ (reloaded at the `9613` loop top); verify-mode 246/0; inert in gameplay.
   `9200` (a hardware DAC-readback fade loop) is still open.
3. **Full-screen image present** — the SQZ-image → planes blit (intro/title/map backgrounds).
   Define the `SceneImage` asset + recover `present_image`.
4. **Menu/title/map scene state** — the scene **state machine** + menu cursor/selection + the
   `draw_cursor` highlight: the top layer that produces `SceneState` (the real backbone).

## Witnesses needed (the gating constraint)

Every snapshot so far is captured *after* a scene draw, so there is no faithful oracle for
text/image/cursor. Each leaf needs a **mid-draw witness**: a state captured *during* the draw
(font segment + VGA state still live). `pre2/probes/capture_text_draw.py` drives a cold boot to
a text screen and captures `draw_string`'s inputs + the before/after planes so it can be
lockstep-verified. The same approach (hook the leaf entry during a cold boot to that scene)
gives witnesses for the image present, the palette install, and the menu cursor.

## Status

* `SceneState` + `render_scene(state, target)` seam — **drafted** (`pre2/recovered/scene.py`),
  with a `RenderTarget` abstraction handling both video modes; composes the recovered
  `draw_string` (text) + `fade_palette` (palette) + `present_image` (linear/planar). Tests in
  `tests/test_scene_render.py`.
* **Text leaf `draw_string` — VERIFIED.** Confirmed (1) by full disassembly `1030:9886`..`98FF`
  and (2) by **runtime lockstep — 24/24 menu text draws byte-exact, 0 divergence** ("MODE",
  "BEGINNER", …), reached by replaying `demo_pre2_20260622_192206` (which navigates the menu) and
  diffing planes 2|3 vs the ASM (`pre2/probes/capture_text_draw.py`). The witness needed a demo
  replay because `draw_string` only fires on menu/score/tally **redraws**, never on cold boot or
  steady gameplay. **Menu findings:** the menu is mode 0Dh; each item is drawn to **both display
  pages** (`0x0`/`0x1FFF`, double-buffered); the **cursor highlight is a shade swap** — the
  selected item is re-drawn with `font_base 0x4200` (vs `0x0`), no separate cursor sprite. So the
  faithful highlight is just a `TextRun` with the highlight shade; `draw_cursor` is reserved for
  the enhanced renderer's own style. **Wired live** (`pre2/checkpoints/text.py` +
  `pre2/bridge/text.py`): the native drawer writes planes 2|3 + the advanced pen, `bx` past the
  terminator, `ds` restored to DGROUP. Proven by a **per-call whole-state lockstep** over 54 menu
  draws — every byte at/above SP (all planes, pen, DGROUP, VRAM) matches the ASM; the only residue
  is dead below-SP stack + the clobbered scratch registers, which all four call sites
  (`9930/994b/9996/99a7`) reload or ignore. Inert in steady gameplay.
* **Image present** — `SceneImage` (linear-13h + planar-0Dh) + `present_image` defined and
  validated on real extracted images; the exact ASM present routine still to be pinned.
* **Scene transitions — already recovered.** The palette fade (`6772`) and the **end-level
  circular iris/vignette** are both recovered in `pre2/recovered/transition.py`. The iris was
  long mis-labelled the "scale/zoom transition": `build_scaled_columns` reads a quarter-circle
  cos/sin table (`[0x7090]`/`[0x6F90]`, `src_x²+src_y²≈64²`) × a shrinking radius `[0x2DD0]`
  about the player (`[0x2DC6]`/`[0x2DC8]`); `draw_scale_frame` clears outside the circle via
  `clear_span`. So the gameplay→tally transition's pixel work is done; only *when* it fires
  (scene logic) is the border.
* **Iris wired live (VERIFIED).** The whole per-frame block `1030:31F4..32B0` (build column
  table → clear outside the circle) is replaced natively by `pre2/checkpoints/transition.py`
  (bridge `pre2/bridge/transition.py`): read radius/centre/clamp/page + cos·sin tables, run the
  recovered `build_scaled_columns` + `draw_scale_frame`, write the four EGA planes +
  scaled-column tables back, continue at `32B0` (an inline fall-through block — only `ip`
  advances, no stack change). The controller after `32B0` reads only `[0x2DC2]/[0x2DC0]/[0x2DD0]`
  and re-renders + `fade_palette`; the planes are the only state it consumes, but the hook also
  writes the block's terminal DGROUP scratch (`[0x2DCC]/[0x2DCE]/[0x2DCA]/[0x2DD2]`) so the
  whole-memory oracle stays exact. Verified three ways: 47 live frames byte-exact
  (`pre2/probes/verify_iris_block.py`); verify-mode lockstep 0 divergence; and a **foolproof
  whole-state** check (hooks-on vs hooks-off stepped to the iris end, full `memcmp`) — identical
  across all live memory + registers, the only residue being dead below-SP stack scratch (the
  ASM's popped `push cx`), unreachable by construction. **It was the transition's slowness** — the
  iris burned ~2.14M interpreted instructions (≈4.8s); the native block is ~17K (≈0.14s), a **34×**
  speed-up — and it is inert during gameplay (`31F4` is reached only at level end).
* Menu cursor + scene state machine — contracts only, to be recovered in the order above.
