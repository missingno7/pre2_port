# Renderer island — scope, border, and completion status

The goal: **completely exhaust the renderer island** — recover everything that belongs
to it into clean VM-independent source, and draw a precise border so we know what does
*not* belong. Addresses are the GOG build (segment `1030`), confirmed by capstone disasm
unless marked otherwise.

## What the renderer island *is*

The renderer is **`game state → pixels/palette`**: it consumes `Camera`/`ScrollState`/
`TileMap`, sprite & object *positions*, and palette/fade *state*, and writes A000 planar
VRAM + the VGA DAC. It does **not** update game state and does **not** own the object/
level data model. The merge target for the whole landmass is a single `update_frame()`.

**Border test:** a routine is in the renderer iff it only *reads* state and *writes*
VRAM/DAC, with no gameplay decision and no ownership of the object/level data model.

## Recovered (in the island)

| Routine | Module | Status |
|---|---|---|
| `4316`/`4389` sprite decode | `recovered/sprite_decode.py` | VERIFIED + live |
| `4232` sprite classify | `recovered/sprite_classify.py` | ASM_MATCHED (verify wired) |
| `3B88` blit_sprite (type 0/1/≥2) | `recovered/renderer.py` | VERIFIED + live |
| `348D` tile-row, `35A1` grid, `3A27` scroll-copy, `3054` panel | `recovered/frame_renderer.py` | VERIFIED + live |
| `26FA` object_render (moving sprites) | `recovered/object_render.py` | VERIFIED + live |
| `32DE` clear_span (transition border wipe) | `recovered/transition.py` | ASM_MATCHED (committed test) |
| `31F4` build_scaled_columns (scale geometry) | `recovered/transition.py` | ASM_MATCHED (40 frames/0 div) |
| `324B` draw_scale_frame (border-clear pass) | `recovered/transition.py` | ASM_MATCHED (15 frames/0 div VRAM) |
| `6772` palette fade (DAC interpolation) | `recovered/transition.py` | VERIFIED + live |
| `3588` calc_scroll_source | `recovered/frame_renderer.py` | ASM_MATCHED (15 calls/0 div) |
| `3668` redraw_animated_grid (animated bg tiles) | `recovered/frame_renderer.py` | ASM_MATCHED (7 frames/0 div) |

## Gaps — renderer, still ASM (to recover)

1. ~~**End-level scale/zoom transition.**~~ **DONE** — the effect is a *shrink-via-border-
   clear* (no image rescale; the `4700` "scaled copy" guess was wrong). All pixel/geometry
   pieces recovered in `recovered/transition.py`, ASM_MATCHED byte-exact: `build_scaled_columns`
   (`31F4-3249`, the per-frame scaled-column table `[0x6B14]`/`[0x6A88]`), `draw_scale_frame`
   (`324B-32AE`, clears the 4 borders of the window shrinking about `([0x2DC6],[0x2DC8])`),
   and `clear_span` (`32DE`). The outer loop `31F4..32DD` (scale `[0x2DD0]` 0xE6 step `[0x2DC0]`,
   per-frame `452B` GC-reset / `26FA` / `4509` page-flip / `44CD` vsync / `6772` fade) is the
   thin controller → folds into `render_frame` (Phase 4).

2. ~~**Palette fade `6772`.**~~ **DONE** — recovered as `recovered/transition.py:fade_palette`
   (+ `bridge/palette.py`, `checkpoints/palette.py`), VERIFIED + live. Linear interpolation
   of 16 colours×3 (48 6-bit DAC components) from a source palette (`[0x2D00 + [0x2D8A]*2]`
   ptr) toward the target `[0xACB7]`, stepping by `[0x6C03]` (incremented per call) until all
   arrive, then clears `[0x6C01]`/`[0x6C02]`. Direction flag `[0x6C02]` swaps src/target.
   56 fade steps / 0 divergence (snapshot 021225) + committed golden test.

3. ~~**Scroll engine helpers.**~~ **DONE** (reached headless by injecting movement —
   right/left arrow 0x4D/0x4B — into gameplay snapshot 185902 to drive a real scroll):
   - **`3588` calc_scroll_source** — `[0x2DBA] = 2*col + 0x280*row + 0x3F40`. RECOVERED
     (`frame_renderer.py:calc_scroll_source`, ASM_MATCHED, committed golden).
   - **`3668` redraw_animated_grid** — the actual hot unrecovered scroll routine: redraws
     the 12×20 grid blitting only animated tiles (flagged in 0x6988), remapped through the
     animation frame `[0x6BC2]`. RECOVERED (`frame_renderer.py`, ASM_MATCHED, 7 frames/0 div).
   - The ledger's `34ED` "column fill" was STALE — it's the recovered `draw_tile_row`'s loop
     tail. The ledger's `3344/338E/33F5` are the scale transition, NOT scroll. The camera
     *advance* (deciding where to scroll) stays on the border (game loop).

4. **Frame compositor `3B40`.** Static glue `draw_grid() → scroll_copy() → panel()`;
   characterized, unwired (no available scenario reaches it). Becomes the renderer's
   `update_frame()` once all its leaves are recovered.

## Border — NOT in the renderer island

- **Object-list iteration + object-draw dispatch** (`34A0`/`3552`/`65A0`/`8BFF`): own the
  `ObjectSlot` data model; they only *call* the blit → **object system**.
- **Object/player update** (movement, AI, physics, collision) → gameplay.
- **Frame conductor / tick dispatch** (decides *when* to call the compositor/transition) →
  game loop. (`update_frame()` is the renderer's top; *who calls it* is outside.)
- **Camera advance** (the directional-scroll deciding *where* to scroll) → game loop/input;
  the scroll *render* (fill exposed row/col) is the renderer.
- **Tally/score logic**, **asset load (SQZ)**, **audio** → separate systems. (Tally screen
  *drawing* is renderer; the *scoring* is not.)

## Completion plan / repro needs

1. Scale transition (002633, headless) — recover next.
2. Scroll engine — re-map `3569`/`34ED` on GOG; needs a horizontal-scroll snapshot.
3. Palette fade `6772` — needs a snapshot captured mid-fade (`[0x6C01]|[0x6C02] != 0`).
4. Wire `3B40 → update_frame()` once its leaves are all recovered; the per-hook coastline
   then collapses to one frame entry.

Each gap follows the standard island workflow (faithful witness → pure module + bridge →
byte-exact verify → thin hook). This file is the checklist; tick items as they land.
