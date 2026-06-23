# The render model — semantic render intent (the foundation under the faithful renderer)

**The question:** *are we still too close to the old machine, or have we recovered enough
semantic render intent?*

**Verdict:** **partway, and it differs sharply by render category.** The *inputs* are
recovered semantically and the *sprite* path is genuinely deep, but the renderer still
**emits EGA planes, not render commands**, the **tile background is pure plane-blit through
the scroll ring**, and there is **no unified frame snapshot**. So on the output / tile /
transition side we are still too close to the machine; on sprites / camera / identity we are
deep enough.

## The target layering

```
VM / oracle framebuffer
  -> recovered video routines (object_render, frame_renderer, transition, present)
  -> RENDER MODEL: a GameFrameSnapshot of render commands   <-- the new semantic seam
  -> faithful rasteriser (the recovered leaves -> EGA planes)   [verified == ASM]
  -> future enhanced renderer (own framebuffer, own clock, interpolation)
```

The render model (`pre2/recovered/render_model.py`) is plain frozen data — object identity,
world/screen pixel positions, sprite ids, palette/transition state, draw order — with **no
VRAM offsets, no plane buffers, no CRTC/ring state**. The planar realisation stays in the
faithful rasteriser as oracle/scaffolding. Verification: `build snapshot -> faithful raster
== ASM`, byte-exact, which proves the snapshot is *complete* (carries everything the pixels
need) — exactly the foundation the enhanced renderer requires.

## Where each foundation stands today

| Foundation (your list) | Status | Where |
|---|---|---|
| Object identity (stable across frames) | **have** | `Sprite.sprite_id`, `base_id = id & 0x1FFF` |
| Object positions (world) | **have** | `Sprite.x/y` (px), now in `SpriteDrawCmd.world_x/y` |
| Sprite ids / metadata | **have** | `SpriteAttr` (w/h, offsets, src seg:off) by id |
| Object render intent | **sprites: have** | `plan_sprite_command -> SpriteDrawCmd` (NEW) |
| Screen-space position | **sprites: have** (now explicit) | `SpriteDrawCmd.screen_x/y` (was folded into `dest_off`) |
| Camera state | **have** | `Camera` / `CameraState` (tiles + fine px) |
| Draw order | **have** | active-list order (`plan_frame`) |
| Clipping/culling decisions | **have (sprites)** | in `plan_sprite` (window + edge clips) |
| Animation state | **partial** | `life` + frame counter drive blink/mode; frame = the id (caller-selected) |
| Palette state | **partial** | `FadeStep(a,b,amount)`; applied straight to DAC, no frame-level `PaletteState` |
| Transition/fade state | **machine-level** | iris/fade recovered as pixel/DAC ops, **not** `TransitionCmd` |
| HUD state | **partial** | id `0x135` special-cased in `plan_sprite` (no model); score/lives = text island |
| Tile/background state | **machine-level** | tilemap+camera are inputs, but background is **blitted via the scroll ring**; no `TileDraw` |
| Render commands (Sprite/Tile/Palette/Transition) | **Sprite only** | `SpriteDrawCmd` done; `TileDrawCmd`/`PaletteState`/`TransitionCmd` are contracts only |
| GameFrameSnapshot | **contract only** | defined; assembled for sprites/camera; tiles/transition pending |
| Hidden VM/CRTC/planar deps isolated | **no** | `RendererState` mixes semantic (camera, tiles) with machine (`col_ring`, `scroll_src`, `dest_page`, `row_ring`, `dirty`) |

## The honest gaps (why we are still too close to the machine)

1. **Output is EGA planes.** `render_frame(state, planes, dac)` writes planar VRAM. The
   faithful renderer's *output* should be a `GameFrameSnapshot`; the planar blit should be a
   separate, verified rasteriser step. Today the "framebuffer" *is* the VGA planes.
2. **The tile background has no command.** `redraw_animated_grid` + `draw_grid` +
   `scroll_copy` blit tile pixels into a scrolling ring buffer driven by ring/CRTC state.
   The semantic background — *tile T at grid (c,r) -> screen (x,y)* — exists only implicitly.
   This is the biggest gap (everything camera-relative and interpolation-friendly is hiding
   behind a byte copy).
3. **`SpriteDraw` was a hybrid.** It carried `sprite_id` (semantic) but folded screen
   position into `dest_off` + post-shift `byte_width` + `shift` (planar). **Now fixed for the
   model:** `plan_sprite_command` emits explicit `screen_x/screen_y` (and `world_x/y`),
   `SpriteDraw` remains the faithful raster command.
4. **Transitions are pixel ops, not state.** The iris knows "circle radius R about the
   player" *inside* `build_scaled_columns`, but a frame can't say "TransitionCmd(IRIS,
   center, radius)". Same for fades.
5. **`RendererState` mixes layers.** Semantic (`camera_x/y`, `tiles`) and machine
   (`col_ring`, `scroll_src`, `dest_page`, `row_ring`, `dirty`, `row_factor`) live together.
   The machine half should be isolated as faithful/oracle detail behind the rasteriser.

## What this change did (first step)

- **`pre2/recovered/render_model.py`** — the semantic contract: `GameFrameSnapshot`,
  `CameraState`, `SpriteDrawCmd`, `TileDrawCmd`, `PaletteState`, `TransitionCmd`, `BlitMode`,
  `TransitionKind`. Pure data; the abstraction the enhanced renderer will consume.
- **Sprite intent lifted** — `object_render.plan_sprite_command` emits `SpriteDrawCmd`
  (identity, world+screen px, graphic, blink mode), sharing `plan_sprite`'s exact placement
  logic via an extracted `_placement` so they can never drift. `plan_sprite` is byte-identical
  (8/8 object-render goldens unchanged). Consistency guarded by `tests/test_render_model.py`.

The sprite path can now answer your questions directly: *what object* (`base_id`), *which
graphic* (`sprite_id` + `src`), *where* (`world_x/y`, `screen_x/y`), *how* (`flip`, `mode`),
*animation phase* (`life`), *draw order* (list), *intent vs VGA detail* (`SpriteDrawCmd` vs
`SpriteDraw`).

## Second step (done) — snapshot assembler + tile render intent

- **`pre2/recovered/render_snapshot.py`** — `build_frame_snapshot(RendererState) ->
  GameFrameSnapshot`: assembles camera (pixel-precise) + palette state + the ordered sprite
  list + HUD + the tile background, the unit two snapshots are interpolated between.
- **`plan_tiles`** — the background as `TileDrawCmd`s (tile id + grid cell + screen position
  + type), the plain "tile T at grid (c,r) -> screen (x,y)" mapping that **replaces the
  scroll-ring machinery** for the model. **Cross-checked in-VM (snapshot 185902): the 240-tile
  enumeration matches the recovered, ASM-matched `redraw_animated_grid` tile walk exactly**,
  and the sprite list matches `plan_frame`. Committed guard: `tests/test_render_snapshot.py`.

So a `GameFrameSnapshot` is now produced from real VM state with semantic camera + tiles +
sprites + palette — no VRAM offsets, no ring buffer. (Still using the faithful renderer for
the *pixels*; the model carries the *intent*.)

## Finding — the background is *layered*, not a flat tile grid

Trying to flat-raster `plan_tiles` against the real screen (gameplay 185902) revealed the
real structure (and corrected the model). The background is **a parallax base layer + a
foreground tile layer composited by transparency type**:

* **Tile graphic source** = the planar cache `CACHE_BASE (0x5E80) + tile_id*0x20`, **packed**
  (row stride = 2 bytes, 32 B/slot = 16×16 px), *not* the screen stride — de-planarise with
  stride 2. (That packing was the bug that first gave noise.)
* **type 0 (opaque)** — plain cache copy. **86% pixel match** vs the page (gameplay 185902),
  so the cache layout + the grid→screen mapping (`tile(col,row) -> (col*16,row*16)`) are
  right; the foreground (rocks / ground / plants) reproduces from the model.
* **type 1** — **0% cache match**: these show the **parallax base layer** (sky / mountain /
  clouds — a separate scrolled bitmap), not a tile graphic.
* **type ≥2 (masked)** — ~61%: the cache graphic's opaque pixels composited over the base
  (`(base AND mask) OR sprite`).

So the visible page = base parallax layer ← foreground tiles (per-type composite) ← sprites ←
HUD bar. `plan_tiles` correctly captures *which tile / where / what type*; the missing model
pieces are the **base layer** and the **composite**.

## Renderer state machines (persistent visual state, not VGA side effects)

The faithful renderer must reach past per-frame drawing into the renderer's **persistent
state machines** — the visual systems that keep evolving while gameplay runs. A palette fade
after an item pickup is the canonical case: it is renderer-owned state with a phase, progress,
and endpoints, *not* a one-off VGA write. Two consecutive `GameFrameSnapshot`s must show it
advancing so an enhanced renderer can smooth it on its own clock.

### Palette state machine — RECOVERED (the first one)

State (DGROUP): `[0x2D8A]` selected **named** palette (index into the `[0x2D00]` pointer
table of level/area palettes) · `[0x6C01]` fade-active · `[0x6C02]` direction (0 = IN toward
the `[0xACB7]` target, 1 = OUT) · `[0x6C03]` progress (0..63) · `[0x6C04]` fade phase flag ·
`[0x6BE6]` palette-changed flag. Controllers (disassembled, labelled):

| Site | Event | Effect |
|---|---|---|
| `1030:6772` `fade_palette` | per-frame **step** | move each component toward target by progress; clear flags when arrived |
| `1030:877x` | **start fade-in / reverse to fade-out** | `[6C01]=1,[6C02]=0,[6C03]=0,[6C04]=1`; reverse → `[6C02]=1` |
| `1030:882A` | **area palette swap** | remap the selector (2↔0xC, 6↔0xE, 0xD→2, 0xF→6) + `[6BE6]=1` — a visual state change |
| `1030:4CC4` | **palette cycle** | `inc [2D8A]` — advance to the next named palette |

Exposed semantically as `render_model.PaletteState` (`colors` = displayed RGB; `phase` ∈
NONE/IN/OUT; `fade_amount`; `fade_from`/`fade_to`), read by `bridge.palette.read_palette_state`.
The fade *math* is byte-exact (`fade_palette`, test_transition); the state read is guarded by
`tests/test_palette_state.py` and exercised live on gameplay snapshots (named index 0/1/2 seen).

### Still to recover (the rest of the renderer's persistent state)

* **Transition state** — the iris (`[0x2DD0]` radius, `[0x2DC6]/[0x2DC8]` centre) as a
  `TransitionCmd(IRIS, …)`; recovered as pixels (`build_scaled_columns`), not yet as state.
* **Screen flash** — the brief palette override on pickup/hit (likely a fast select+restore).
* **Camera state machine** — shake / recentre / lock, beyond the static `CameraState`.
* **Animated-tile phase** — the `[0x6BC2]` animation-frame counter (drives `anim_xlat`).
* **HUD/layer state** — score/lives/energy as a HUD layer, not ad-hoc sprites.

## Roadmap (remaining — in order)

1. **Tile background — finish (now well understood).** Recovered: tile enumeration
   (`plan_tiles`) + the tile graphic source (cache, stride 2) + the grid→screen mapping
   (type-0 verified 86%). Remaining: (a) the **parallax base layer** as a model element (the
   scrolled bitmap type-1 tiles reveal; it lives at the `bg_off` region `di + 0x7E80`), (b)
   the **type-aware composite** (0=opaque, 1=base, ≥2=masked) in a flat rasteriser verified
   pixel-exact vs the page, (c) the sub-tile *horizontal* scroll into the camera (only vertical
   `fine_scroll` folded so far). That fully frees the background from the ring.
2. **Transition as state.** `TransitionCmd(IRIS, centre, radius)` / `(FADE, amount)` emitted
   per frame (palette is already exposed as `PaletteState`), so the enhanced renderer smooths
   them on its own clock. Today `build_frame_snapshot` always emits `TransitionKind.NONE`.
3. **The capture seam.** Hook the main-loop top (`0214`) to call `build_frame_snapshot` once
   per tick and keep the last two — the interpolation input (see
   `native_renderer_feasibility.md`; the lerp PoC already works on `RendererState`).
4. **HUD model.** Fold the `0x135` fixed-screen path + the text/score island into a HUD
   command list (no camera, no interpolation).
5. **Split `RendererState`.** Separate the semantic camera/tile inputs from the machine
   ring/CRTC bookkeeping so the latter is clearly oracle-only.

Only after this is the enhanced renderer safe to build: it consumes `GameFrameSnapshot`s,
owns its own present clock, and smooths/interpolates **things grounded in recovered game
state** — not guessed pixels or framebuffer tricks.
