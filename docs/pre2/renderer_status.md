# Renderer island — completion status (live working notes)

Running status for the "finish the renderer island" goal. Updated as islands land.
Companion to `renderer_island.md` (the map/border) and `renderer_goal.md` (the plan).

## Phase 1 — reconnaissance (done this pass)

Profiled all 24 snapshots + the gameplay snapshots (185902, 212037) in hybrid, and
re-disassembled the scroll/grid region on GOG. Findings:

- **The steady-state renderer is largely recovered.** In gameplay the hot ASM is NOT
  the renderer — it is the **object system**: `0x69xx`/`0x89xx`/`0x67xx`/`0x68xx`/
  `0x80xx–0x86xx` (object update + the object-draw loops `65A0`/`8BFF`, ObjectSlot
  `0x83EF`). These are **outside the renderer border** (they own the data model and only
  *call* the blit). The remaining `1Cxx` (~45%) is the idle frame-governor spin.
- **Shared blit reused by the object system.** `2C00–2DFF` (object_render's shifted/
  masked blit) is also entered by the object-draw path, so it shows hot even though
  `26FA` is recovered+live. The blit *logic* is recovered (paint_sprite); the object
  system calling the ASM copy of it is the **renderer↔object boundary** (recover with
  the dormant `653D` object_draw once the object system feeds it).
- **Scroll/grid addresses re-mapped (GOG):**
  - `35A1` `draw_grid` (recovered+live) — its inner draw loop is `~353A–3587` (calls
    `3B88` blit; the ledger's "calc-scroll-src 3569" is actually *inside this loop*).
  - **`3588`–`35A0` = calc scroll source** (`[0x2DBA] = camera·… + 0x3F40`) — the real
    GOG "calc scroll src" (ledger's `3569` was stale). Small, OBSERVED, **gap**.
  - The ledger's directional-scroll `3344/338E/33F5` are **stale on GOG** (that range is
    the scale transition). The directional scroll proper still needs locating via the
    call graph from the camera-advance; the per-frame *fill* it calls is `348D`
    (recovered) / its vertical counterpart `34ED` (gap, confirm on GOG).

## Gaps — current status

| Gap | GOG addr | Reproducible? | Status |
|---|---|---|---|
| Scale/zoom transition | `31D0` loop = build `31F4-3249` + draw `324B-32AE` + span-clear `32DE` | **YES** (002633, 173821) | **RECOVERED + ASM_MATCHED** — all three pixel/geometry pieces: `clear_span` (32DE, 1073 spans/0 div), `build_scaled_columns` (31F4, 40 frames/0 div), `draw_scale_frame` (324B, 15 frames/0 div byte-exact VRAM). Committed tests. Remaining outer-loop bits (`452B` GC-reset, `4509` page-flip, `44CD` vsync, scale-decrement) are presentation plumbing → fold into render_frame in Phase 4. **There is no separate "scaled image copy" — the effect is shrink-via-border-clear; `4700` is unrelated.** |
| calc-scroll-source | `3588` | **YES** (185902 + injected movement) | **RECOVERED + ASM_MATCHED** (`frame_renderer.py:calc_scroll_source`, `[0x2DBA]=2*col+0x280*row+0x3F40`; 15 calls / 0 div, committed golden test) |
| ~~vertical tile-column fill `34ED`~~ → **animated grid redraw** | `3668` (entry; loop `36B3-3715`) | **YES** (185902 + injected scroll) | **RECOVERED + ASM_MATCHED** (`frame_renderer.py:redraw_animated_grid`, 7 frames/0 div VRAM+state). The ledger's "34ED column fill" was STALE — `34ED` is just the recovered `draw_tile_row`'s loop tail. The real hot unrecovered routine during scroll was `3668`: redraws the 12×20 grid but blits only the *animated* tiles (flagged in table 0x6988), each remapped through the animation frame `[0x6BC2]`; all type-0 opaque; reuses the verified blit `3B88`. Throttle + anim-advance (`3668-36A6`) is the thin controller. |
| Palette fade | `6772` | **YES** (021225, user-captured mid-fade) | **RECOVERED + VERIFIED + live** (`recovered/transition.py:fade_palette`, `bridge/palette.py`, `checkpoints/palette.py`): 56 fade steps / 0 divergence in-VM lockstep + exact done-correspondence; committed golden test |
| object-draw render | `653D` (recovered, dormant) | needs object system to drive it | renderer↔object boundary |
| frame compositor → update_frame | `3B40` | not reached in any snapshot | wire once leaves recovered; verify offline |

## span-clear `32DE` — fully decoded (ready to recover)
Clears pixels `[x, x+width)` at screen row `dx`, all 4 planes (caller sets SC map mask
0x0F). VRAM byte `= row*0x28 + [0x2DD8] + x>>3`. Bounds: `x<0x140, width<=0x140,
row<0xC8`. Left partial: `&= ~(0xFF>>(x&7))`; full bytes `= 0`; right partial:
`&= 0xFF>>((width + x&7)&7)`. (Aligned + width<8 → only the right-partial path.)

## Phase 4 — consolidated `render_frame(RendererState)` seam (built)

The recovered leaves are consolidated into one VM-independent entry point — the
**replaceable-renderer seam**:
- `pre2/recovered/render_frame.py`: `RendererState` (plain-data input contract) +
  `render_frame(state, planes, dac)`, composing the leaves in the original per-frame order
  **palette fade (6772) → animated-grid (3668) → grid (35A1) → scroll-copy (3A27)**.
- `pre2/bridge/render_state.py`: `read_renderer_state(mem)` reconstructs `RendererState`
  read-only (reuses the frame/palette readers).
- A future native enhanced renderer drops in by reimplementing `render_frame` against the
  same `RendererState`.

**Per-frame order** (traced in-VM, gameplay 212037/185902): each frame fires
`fade → animgrid → grid → scroll → objs(26FA)` exactly once (no panel/compositor in steady
state). `RendererState` is captured at the post-controller instant (after the camera +
animation-frame `[0x6BC2]` advance — the grid-loop entry `36B3`).

**Standalone proof:** `render_frame` reproduces the renderer-owned **background ring buffer
byte-exact with NO VM stepping** — 0 divergence across steady AND grid-redraw frames
(212037 + 185902). Committed composition test `tests/test_render_frame.py`.

**Border confirmed (by profiling a steady frame):** the residual full-screen differences are
the **object system** (`65A0`/`8BFF` iterating the ObjectSlot data model → the *shared* blit
`2C00`) layering gameplay sprites on top. That owns gameplay state and is **outside** the
renderer (exactly the border in `renderer_island.md`). So `render_frame` produces the
renderer's contribution (bg + scroll + palette); the moving-sprite *list* pass `26FA` layers
via the recovered `object_render`; the object system layers gameplay sprites separately.

**Remaining for a full-screen standalone proof:** fold `26FA` into `render_frame` from
`RendererState` (it currently reads its list via its own bridge), and collapse the 5 live
leaf hooks into one `render_frame` entry hook (the coastline's final step). The compositor
`3B40`/`3B5F` (draw_grid→scroll→panel) static path is still unreached in any snapshot.

## NEEDS REPRO (for the user)
- **Palette fade**: ~~F12 mid-fade~~ — **DONE** (snapshot 021225 supplied; fade recovered + verified).
- **Horizontal scroll**: F12 while moving left/right across a wide level (to witness the
  vertical column-fill `34ED` + the directional scroll).
- **Cold-start screen transitions**: from a fresh launch, press a key to dismiss the
  "oldies"/title screen — the user noted these expose extra transitions worth snapshotting.

## Border (confirmed by the gameplay profile)
The object system (update + object-draw loops) is the dominant non-idle ASM in gameplay
and is **out** of the renderer — exactly the border drawn in `renderer_island.md`. The
renderer's remaining work is the **effects** (scale transition, palette fade), the small
**scroll helpers** (`3588`, `34ED`), and the **object-draw boundary** (`653D`/shared blit).
