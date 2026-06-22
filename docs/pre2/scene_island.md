# Scene island — the non-gameplay visual seam (`SceneState → render_scene`)

The gameplay frame collapsed into a meaningful seam: `RendererState → render_frame(...)`.
The startup / title / menu / map / loading / tally screens need the **same kind of target**
so we recover their *visual intent*, not a pile of isolated VGA hooks:

```
scene logic / state machine        (BORDER — owns "which screen", input, transitions)
   -> SceneState                    (this contract: a plain-data description of the screen)
   -> render_scene(state, target)   (FAITHFUL leaves: image, text, cursor, palette)
   -> planar VRAM + 16-colour DAC   (faithful, verified vs the VM/VGA oracle)
        |  enhanced: same SceneState -> native renderer (own buffer, no VGA/CRTC/page flip)
```

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
   plumbing that frames a scene draw. Recover as the faithful present + the palette leaf.
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

* `SceneState` + `render_scene(state, planes, dac)` seam — **drafted** (`pre2/recovered/scene.py`),
  composing the recovered `draw_string` (text) + `fade_palette` (palette); `present_image` +
  `draw_cursor` are provisional contracts.
* Text leaf — recovered, plugged into the seam; **verification pending** a mid-draw witness.
* Everything else — contracts only, to be recovered in the order above.
