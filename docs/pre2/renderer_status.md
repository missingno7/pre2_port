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
| frame consolidation → `render_frame` | `3B40`/`3B5F` (ASM static subset, unreached) | n/a | **DONE**: `render_frame(RendererState)` built + proven standalone (Phase 4 below). `3B40`/`3B5F` is just one unreached ASM caller of a leaf subset |

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

**Moving-sprite pass folded in:** `render_frame` now also runs the `26FA` pass
(`plan_frame` → `paint_sprite` over the active-sprite list, bundled in `RendererState` via
`read_object_render_inputs`), layered on the scrolled background. So the order is
`fade → animgrid → grid → scroll → objs(26FA)`.

**Standalone proof:** `render_frame` reproduces the renderer-owned output **with NO VM
stepping** — the background ring buffer is byte-exact (0 div across steady AND grid-redraw
frames, 212037 + 185902); the full framebuffer at the `26FA` RET matches except a fixed
single-bit residual that is the **object system** (gameplay sprites via `65A0`/`8BFF` → the
shared blit, the documented border — *not* the special `0x135` HUD sprite, which is absent
here). RendererState must be captured at the post-controller instant (after the camera +
animation-frame `[0x6BC2]` advance = grid-loop entry `36B3`). Committed composition tests
`tests/test_render_frame.py`.

**Border confirmed (by profiling a steady frame):** the residual full-screen differences are
the **object system** (`65A0`/`8BFF` iterating the ObjectSlot data model → the *shared* blit
`2C00`) layering gameplay sprites on top. That owns gameplay state and is **outside** the
renderer (exactly the border in `renderer_island.md`). So `render_frame` produces the
renderer's contribution (bg + scroll + palette); the moving-sprite *list* pass `26FA` layers
via the recovered `object_render`; the object system layers gameplay sprites separately.

**Single-hook collapse — why it's a post-VM step (verified):** the main loop is one
conductor at `1030:0214-0270` that calls the renderer leaves **individually and interleaved
with game logic** — `…game systems… → animgrid(3668)@0241 → grid(35A1)@0244 →
scroll(3A27)@0247 → 4b8e → objs(26FA)@024d → 3721/54ab/3922/4c69 → 45af/44fb →
fade(6772)@0267 → … → jmp 0214`. There is **no single ASM function** that runs only the
render block, so the 5 leaf hooks **cannot** be collapsed into one live hook in the hybrid
(doing so would skip the interleaved game logic). The collapse happens **post-VM**: a native
renderer replaces the main loop's render calls with one `render_frame(read_renderer_state())`.
`render_frame` is exactly that drop-in seam. (Fade is called last in the loop, not first as
in `render_frame`, but it is DAC-only so the order is pixel-equivalent.) The compositor
`3B40`/`3B5F` static path remains unreached in any snapshot.

## Phase 3 — cleanup status
- **`read_active_list` "off-by-one": NOT a bug — do not change.** Verified vs ASM: 1030:270C
  sets `si = 0x5720` (LIST_TOP) and 2713 processes that record first; the top slot is a
  genuine processable slot (empty today → handled by the per-record `sprite_id == 0xFFFF`
  skip). Starting at `LIST_TOP - RECORD_BYTES` would drop a sprite whenever the top slot is
  occupied. Code comment added. (The review's hypothesis was wrong — the lockstep is the authority.)
- **object_render record-mutation split: DONE.** `plan_record_update` (recovered) +
  `write_record` (bridge) make the per-frame record mutation (life dec + drawn bit) explicit;
  the checkpoint applies it instead of re-deriving inline, and **verify mode now diffs the
  record (flags/life) against the ASM** — 0 divergence on all 5 protected snapshots. The
  recovered mutation is byte-exact vs the ASM.
- `pre2/probes/` kept: the `verify_*.py` are the documented in-VM lockstep harnesses (the
  proof the docstrings point to), not throwaway; this session's captures were all inline.
- Remaining (lower priority, non-blocking): coastline shortening, merge-target taxonomy.

## NEEDS REPRO (for the user) — only optional border subsystems remain
- **Palette fade**: ~~mid-fade~~ — **DONE** (021225).
- **Horizontal scroll**: ~~needed~~ — **DONE** (reached via injected movement on 185902;
  `calc_scroll_source` + animated-grid `3668` recovered).
- **Particle/effect system** (`4b8e`, border): a snapshot with active particles
  (`[0x7DE6] != -1` — explosion / hit-spark / collectible sparkle frame) would let the small
  particle subsystem be recovered. Empty in every available snapshot.
- **Special HUD sprite `0x135`** (object_render's no-camera path `2784`, border): a frame where
  an `id 0x135` (`bx==0x26A`) sprite is in the active list (absent in current snapshots). It's an
  **8×12 sprite drawn at a FIXED screen position** (the `2784` path skips the camera-X subtract):
  effectively a small **green capsule/pill** (fill = colour `A`, frame = colour `6`; attr
  `[0x7190]`=8×12, src `650A:0BC7`) — **almost certainly a boss health-bar segment** (per the
  user; repeated fixed-position pills). The id is computed (no literal `0x135` in the code), so
  it must be witnessed live (a boss fight), not traced statically.
- **Cold-start screen transitions**: dismiss the "oldies"/title screen from a fresh launch.

## Border (confirmed — full per-frame main-loop classification)
The per-frame main loop `1030:0214-0270` is fully classified (see `renderer_island.md`):
the renderer leaves are `animgrid(3668) → grid(35A1) → scroll(3A27) → objs(26FA) → fade(6772)`
(all recovered, composed by `render_frame`); everything else is **border** — the object
system (`65A0`/`8BFF`, dominant gameplay ASM), the particle/effect system (`4b8e`), the
auto-scroll script (`3922`), the tile-flag trigger (`3721`), and the other game systems
(`6822`/`6210`/`60fe`/`4907`/`5850`/…). The renderer island is exhausted: every exercised
renderer routine is recovered; the only un-recovered draws (`4b8e` particles, `0x135` HUD
sprite) are border subsystems with empty/absent data in all available snapshots.
