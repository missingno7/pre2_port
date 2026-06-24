# Scene island — the non-gameplay visual seam (`SceneState → render_scene`)

> **★ GOVERNING ORDER — HOOK-FIRST (2026-06-24, supersedes any "build the seam first" / "recover
> visual intent, not isolated VGA hooks" framing elsewhere in this file).** Every remaining
> scene/rendering piece is recovered in THIS order, FaithfulVisual LAST:
> ```
> original ASM producer  ->  checkpoint/probe  ->  recovered leaf  ->  live replacement hook
>     (when the contract is stable)  ->  FaithfulVisual consumer (LAST, reuses the SAME leaf)
> ```
> The goal is to **reduce ASM responsibility in the hybrid runtime first**. One visual behavior = one
> recovered leaf = many adapters (live hook + verify probe + FaithfulVisual + later enhanced). FaithfulVisual
> is the **umbrella OVER already-grounded leaves**, NOT where new rendering behavior is invented. Do NOT
> build a scene FaithfulVisual compositor first and ground it later. The `SceneState → render_scene` seam
> below is the *shape* FaithfulVisual converges to, but it is reached by CONSUMING grounded leaves — not by
> composing a from-scratch renderer. (Proven shape: OLDIES = blit_char leaf → live hook 0C3E → FaithfulVisual
> consumes it; game-over = 9C87; tally = 51A3.)
>
> **The nuance (both extremes are wrong) — CONVERGENCE, not a pile of either.** Hooks are *roots*: they
> discover + verify the real original routines; they are NOT the final shape (don't keep a permanent pile of
> tiny VGA hooks). FaithfulVisual is NOT a pile of hooks either, and NOT a place to invent visual intent.
> The endpoint is **recovered high-level source** (leaves + controllers) that BOTH the hybrid runtime (via a
> live hook) AND FaithfulVisual consume. Sequence: hook-first discovery → source-level recovery → runtime
> replacement → FaithfulVisual composition → **later collapse into larger recovered islands**. COLLAPSE only
> with EVIDENCE from the real original call graph (several hooks shown to belong to one real
> routine/controller/compositor) — never collapse to an invented modern design.
>
> So the "0Dh background BLOCKED / from-scratch rebuild reaches ~11%" note below is the blocker for the WRONG
> (faithful-first) approach. Hook-first reframe: ground the runtime PRODUCERS (scroll_blit / scroll_shift /
> present) live and have FaithfulVisual consume them.
>
> CARTE status (corrected 2026-06-24 after a feasibility check): its RENDER LEAVES are grounded —
> `scroll_blit` (965A) is live + `present_pan_flip` (9613) is recovered+verified; the heavy carte ASM is
> JOYSTICK input (game-port 0x201 timing loop, 0D00-0F80) + controller/pacing (4500/1C65), NOT rendering;
> no object/text producers fire. BUT the FaithfulVisual COMPOSITION is BLOCKED, same class as the menu: the
> carte bg is a STATEFUL circular ring buffer (an initial full-page fill at carte load + per-frame
> `scroll_blit` refills), so a from-scratch leaf-replay reproduces only ~37% of the page (diff 20260/32000).
> A from-scratch carte compositor would be INVENTING — do not. The correct (evidence-based) path: (a) trace
> + recover the carte's INITIAL FULL-PAGE FILL producer (runs once at carte load, like game-over's 9B66 —
> currently UN-grounded, missed by the mid-scroll trace); (b) FaithfulVisual maintains a PERSISTENT page
> seeded by that fill and updated each frame by `scroll_blit_column` (the real stateful model, matching the
> game). The MENU then = the same persistent-page model + `scroll_shift`. Both stay BLOCKED on this
> stateful-page seam until it is built; do not guess a from-scratch rebuild.

> **★ STATUS 2026-06-24 (reconciled).** Gameplay + transitions (iris, fade, curtain) + HUD: grounded.
> Non-gameplay scenes grounded hook-first since: **game-over (9C87), tally (51A3), OLDIES (0C3E) are
> live-grounded** + composed by FaithfulVisual; the **title/intro 13h IMAGE is RESOLVED** (codec =
> `unpack_sqz`; `render_title_image` Δ=0; 13h faithful path wired — the old "source unidentified" claim was
> stale). `render_visual` never falls back to ASM VRAM (an unrecovered scene = a LOUD `FaithfulVisualGap`).
> The ONLY remaining faithful-visual gaps are the two **0Dh scrolling-scene COMPOSITIONS — mode-select menu
> and map/carte** — taxonomy **#5 blocked on a history-dependent buffer** (stateful circular ring). Render
> leaves are grounded; the grounded next step is the recovered **initial full-page-fill producer** (a #4
> gap) + a persistent-page model — NOT a from-scratch rebuild (carte ≈37%, menu ≈11%; see #3/#5 in
> `renderer_bug_table.md`). Do NOT guess a second theory.

The gameplay frame collapsed into a meaningful seam: `RendererState → render_frame(...)`.
The startup / title / menu / map / loading / tally screens converge to the **same kind of target** —
but per the GOVERNING ORDER above, that seam is reached by grounding the runtime producers as recovered
leaves + live hooks FIRST, and having FaithfulVisual consume them LAST (NOT by composing a from-scratch
renderer). The seam is the *shape*, not the starting point:

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

## Phase-A faithful-visual integration findings (2026-06-23)

Routing all labeled scene witnesses through `render_visual` (the dispatcher) located the remaining
scene work precisely (no silent fallback — each unrecovered scene raises `FaithfulVisualGap`):

* **Curtain (room/cave-enter) transition — CLOSED 2026-06-24 (no faithful leaf needed).** Resolved with
  the mid-Cave witness `231731`: `1030:3054` is the **per-frame double-buffer page-flip** (one call/frame,
  alternating pages; inner strip copy `1030:309B`, `si+=[0x2DD8]` `di+=[0x2DD6]`). It copies 2-byte ×
  0xB0-row **vertical strips** centre-outward (`0x14±2k`, k=0..9), vsync-paced — and that reveal happens
  **entirely within one call** (~10 internal vsyncs), a SUB-FRAME CRT effect. By the main-loop frame
  boundary `1030:6772` the flip is complete, so the committed front page is always a *whole* frame that
  `render_frame` reproduces byte-exact (proven: partially-revealed boundary `disp_black=17%` → mirror
  Δ=0%). So: `panel_copy` is the verified full-copy oracle (now `completed_pairs`-capable for an optional
  sub-frame mirror); `checkpoints/frame.py:frame_panel_copy` PASSTHROUGH gives the live vsync-paced reveal;
  the faithful boundary mirror needs **no separate curtain leaf**. (Iris likewise already reused by
  `render_visual`.) See `faithful_visual_layer.md` status section + `renderer_bug_table.md` #5.
* **`transition_fade_003841` = a PALETTE FADE-TO-BLACK** (captured at `1030:92C1`: `in al,dx; and
  al,0x3f; out dx,al` DAC clamping), a scene-flow fade — NOT the curtain (earlier mislabeled a "diagonal
  wipe"). It is the recovered `fade_palette` behavior on the live DAC. `palette_fade_021225` routes to
  `GAMEPLAY` and renders via `render_frame` with the fade on the live palette — visually present
  (bucket-3 orchestration, not a visual gap).
* **Mode-select MENU scene (`modeselect_075918`) — fully located, a bounded island like gameplay:**
  - BACKGROUND: a scrolling tiled pattern (master segment `[0x2875]`) panned via CRTC + blitted
    column-by-column — the recovered `pre2/recovered/present.py` (`compute_display_start`,
    `scroll_blit_column`, `scroll_shift_frame`) + `bridge/present.py`. A **clean-framebuffer rebuild
    path** (analogous to gameplay's `build_background_ring`) is the first sub-step.
  - TEXT: a FIXED sequence of 4 `draw_string` (9886) calls at `1030:9920..99A7`, with string pointers
    at `[0xB170]/[0xB175]/[0xB180]/[0xB185|0xB18E]`, pen positions as code immediates
    (`0xC38`/`0xAF2`/`0x12C9`), advance `[0xB1AB]` (3/4), font `[0x2875]`, page `[0xB1A1]/[0xB1A3]`.
    The **highlight** is a shade swap gated by `[0xB197]` (selected item redrawn at a different
    `font_base`). Reuses the recovered `draw_string` (text.py) + `bridge/text.py` reader.
  - PALETTE: a static 16-colour DAC.
  - SceneState to build (bridge `read_scene_state`): background present inputs + the 4 text runs
    (strings + pens + advances + highlight gate) + palette + page-flip bookkeeping.
  - WITNESS GAP: the current menu snapshots are captured *post-draw* (the per-call pen/shade state is
    final), so byte-exact TEXT verification needs a **mid-menu-draw witness** (like the gameplay
    proofs) or driving the menu loop. The background rebuild can be verified from the post-draw page.

NEXT (menu SCENE leaf, in order): (1) recover the menu-background clean rebuild (present-pattern) and
verify it against the witness page; (2) build `read_scene_state` for the menu (background + 4 text
runs + highlight + palette); (3) wire a `SceneKind.SCENE` menu render into `render_visual`; (4)
verify byte-exact (text needs a mid-draw witness). The WIPE transition + the IMAGE (intro/title) +
map/loading/tally/game-over scenes follow as separate leaves.
