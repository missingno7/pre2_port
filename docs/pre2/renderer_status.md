# Renderer island — completion status (live working notes)

Running status for the "finish the renderer island" goal. Updated as islands land.
Companion to `renderer_island.md` (the map/border) and `renderer_goal.md` (the plan).

> **★ 2026-06-24 — the current plan lives in [`faithful_visual_layer.md`](faithful_visual_layer.md)
> ("CURRENT PLAN & STATUS" section).** Deltas since the notes below:
> - **Architecture = one recovered leaf, many adapters** (runtime hook + verify checkpoint + faithful
>   mirror + later enhanced), with **bidirectional convergence** (bottom-up grounding *and* top-down:
>   ground any mirror-used leaf that lacks a checkpoint). `FaithfulVisual.render_visual` is the umbrella.
> - **Frame-boundary mirror** is the canonical live path now: capture at 6772 (`bridge/game_visual_state.py`)
>   so the mirror matches the *displayed* page (cave witness 231731 Δ=0). `render_gameplay_planes` /
>   `render_visual_planes` (`live_render.py`) remain probe entry points and now share `retarget_page`.
> - **Curtain** = sub-frame page-flip → no faithful leaf needed (mirror reproduces boundary frames Δ=0).
> - **HUD** grounded by a registered verify checkpoint (`checkpoints/hud.py`); `effective_bonus_mask` is
>   now a recovered leaf (was bridge logic).
> - **Every gameplay/transition leaf has a registered checkpoint, and the non-gameplay scenes are now
>   grounded hook-first:** game-over (9C87), tally (51A3), OLDIES glyph (0C3E) are **live-grounded** +
>   composed by FaithfulVisual; the **title/intro 13h IMAGE is RESOLVED** (codec = `unpack_sqz`,
>   `render_title_image` Δ=0, 13h faithful path wired — the old "source unidentified" gap was stale).
>   The ONLY remaining faithful-visual gaps are the two **0Dh scrolling-scene COMPOSITIONS — CARTE/map and
>   mode-select menu** — taxonomy **#5 blocked on a history-dependent buffer** (a stateful circular ring;
>   from-scratch rebuilds are WRONG — carte ≈37%, menu ≈11%). The grounded next step is the recovered
>   **initial full-page-fill producer** (a gap) + a persistent-page model, NOT a from-scratch compositor.
>   Everything else is deferred cleanup (palette-fade ownership, `GameFrameSnapshot`→`GameVisualState` Phase C).
>   See `AGENTS.md` (north star + status taxonomy + collapse rule).

## STATUS (2026-06-23): clean-framebuffer normal-gameplay composition COMPLETE

`render_frame(RendererState, planes, rebuild=True, game_root=...)` produces the **complete normal
gameplay frame from a CLEAN (zeroed) framebuffer**, from explicit `RendererState` + named assets,
with **no dependence on ASM-populated VRAM / scroll-ring / temp memory**. Every composed system is
verified **byte-exact vs the ASM page on every current witness**:

| System | How sourced | Verified (witness) |
|---|---|---|
| Background (parallax base + opaque + dynamic tiles) | `build_background_ring` (rebuild) + `asset_planes` (bridge-fed level assets) | 0 div over viewport rows 0–175 (185902, mapscroll) |
| Moving sprites (player/enemies/pickups/popups) | object pass `plan_frame`→`paint_sprite`, banks in `RendererState` | object goldens + live lockstep |
| Palette / fade | `fade_palette` + `PaletteState` | fade math 0 div; live colours == DAC |
| HUD chrome (panel) + dynamic overlay (lives/score/hearts) | `HudChromeAsset` from **ALLFONTS.SQZ** (persistent asset) + `draw_hud` | HUD strip 0/3680 (185902) |
| Boss health meter (vertical bars) | N× HUD sprite `0x135` via the object pass | band 0/640 (192126 full=8, 192140 less=5) |

**The clean-framebuffer composition seam is closed for all observed systems.** Stop hunting for
hidden normal-frame render pieces unless a *new visual witness* proves one.

**Watch-list (RESOLVED 2026-06-24):** the **effect systems are recovered + wired** — see the wrap-up
below. `4B8E` (point particles), `54AB` (firefly swarm), and `3721`/`37F7` (foreground-tile z-order)
all have their own blits and are now composed on top of the core frame via `GameplayEffects`.

## ★ GAMEPLAY RENDERER WRAP-UP (2026-06-24) — core + effects complete, unified seam

The gameplay renderer is **complete and byte-exact**. `render_game_visual_state(gvs)` now yields the
WHOLE displayed gameplay frame in one call: the core `render_frame` (background + sprites + HUD + fade)
PLUS the three effect overlays, all reusing the same recovered leaves the checkpoints verify.

**Effect overlays** (drawn over the core frame; captured at their own hook instants because they have
transient state) — unified in `bridge/gameplay_effects.py` (`GameplayEffects` bundle →
`apply_gameplay_effects`), folded into `GameVisualState`, shared by both faithful render paths
(the 6772 boundary capture and the governor-live death-gap path — no more duplicated draw code):

| Overlay | ASM | Recovered | Capture point | Proof |
|---|---|---|---|---|
| Point particles | `4B8E` | `particles.draw_particles` | 4B8E entry (pre-kill stash) | `verify_particles.py` Δ=0; spider 102733 |
| Foreground tiles (z-order) | `3721`+`37F7` | `foreground_tiles.render_foreground_tiles` | 3732 pass entry (stash) | `verify_foreground_tiles.py` Δ=0; bush 110346 (player behind bush) |
| Firefly swarm | `54AB` | `fireflies.draw_fireflies` + `firefly_sim.step_fireflies` | read at 6772 (slots persist) | `verify_fireflies.py` / `verify_firefly_sim.py` Δ=0; 140330 |

**Firefly perf replacement:** `54AB` is also a **native replacement** (`checkpoints/fireflies.py`) — the
recovered `step_fireflies` owns the whole pass (RNG-driven flocking + draw), so the VM skips the
interpreted routine (~3000 instr/frame). Byte-exact incl. the two SHARED RNGs (`26CF`/`39DF`).

**Verification:** the unified `render_game_visual_state` with effects is Δ=0 over the gameplay viewport
on every witness (140330 fireflies; 110346 moving = foreground tiles + fireflies). Suite 252; 34 islands.

**NON-GAMEPLAY SCENES — mostly grounded (hook-first).** Done + live-grounded + composed by FaithfulVisual:
game-over (9C87), tally (51A3), OLDIES (0C3E), title/intro 13h IMAGE (`render_title_image`, faithful path
wired). **The ONLY remaining faithful-visual gaps are the two 0Dh scrolling COMPOSITIONS — mode-select menu
and map/carte** — taxonomy **#5 blocked on a history-dependent buffer** (stateful circular ring; the grounded
next step is the recovered initial-fill producer + a persistent-page model, NOT a from-scratch rebuild). See
bug-table #3/#5 and `faithful_visual_layer.md`.

### LIVE FAITHFUL PATH (2026-06-23) — promoted from offline/test to a live authoritative renderer

The gameplay renderer is no longer only an offline/snapshot/test island. `pre2/bridge/live_render.py`
`render_gameplay_planes(mem, dos, game_root)` reads an explicit `RendererState` from live VM memory
each frame and renders the visible frame into a CLEAN framebuffer via `render_frame(rebuild=True)`,
deplanarized by `sdl_view.render_planar_rgb_from_planes`. The viewer flag **`--faithful`** displays
that recovered output instead of ASM-populated VRAM (the VM still runs as oracle/state-producer);
**`--faithful-verify`** shows the per-frame divergence vs the VM page in the title bar. The faithful path
**never reads the VM framebuffer**: recovered scenes (gameplay, iris, game-over, tally, OLDIES, 13h images)
render from recovered source; an unrecovered scene (the menu/map 0Dh compositions) raises a **LOUD
`FaithfulVisualGap`** (a diagnostic frame), NOT a silent ASM-VRAM fallback. (The earlier "fall back to the
VM frame" description was stale — there is no VM-framebuffer fallback in faithful mode.)

PROOF (`pre2/probes/verify_live_faithful.py`, vs **pure ASM** = the true oracle, sampled at the
object-pass RET 2DF9 where state↔page are phase-aligned): the gameplay viewport (rows 0–175) is
**byte-exact** on a settled scene (boss frame 192126: 0/28160 every frame) and within a
≤single-sprite-edge residual on a fast-motion scene (185902 falling player: ≤5px after settling) —
that residual is a live-sampling artifact (the object pass mutates each record's blink/life `[+0x11]`
as it draws, so state read at the RET is a hair off-phase for one sprite), NOT a renderer defect.

ARCHITECTURE NOTE: keep growing the faithful renderer as ONE deeply-rooted island (render_frame is
the single seam; don't accumulate disconnected render routines). The long-term faithful visual layer
extends beyond gameplay to a `GameVisualState`/`SceneFrame` family (Intro/Menu/Map/Gameplay/
Transition/Ending), each grounded in the oracle — see `scene_island.md`. The ENHANCED renderer stays
separate: it consumes the verified state/model and is NOT byte-diffed; do not build it on guesses.

**Remaining work is now STATE OWNERSHIP / CONTROLLER RECOVERY, not normal-frame composition.** The
renderer can *display* the game; the next roots explain *who creates the displayed state* — see
`renderer_goal.md`/the symbol ledger: (1) wire the already-verified controllers (`advance_animation`,
camera-shake apply) from *read* → *owned*; (2) scope the object-update system `65A0`/`8BFF` (the
active-list producer) with a disciplined side-effect map *before* making it authoritative; (3) the
iris/transition controller (scene-gated). The faithful layer stays byte/state-verifiable against the
oracle; a later enhanced layer may be non-byte-identical but must consume this verified state.

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
- **Boss-meter / HUD sprite `0x135`** — **RECOVERED + VERIFIED** (`plan_sprite`'s no-camera branch,
  `1030:2784`): drawn at a FIXED screen position `screen_x = world_x - x_off`, `screen_y = world_y +
  y_off` (no camera / row_factor / fine_scroll), skipping the off-screen-X and `screen_y<=0` culls.
  The boss health meter is **N instances** of `0x135` (one per health unit) = vertical teal bars
  bottom-left just above the HUD. **VERIFIED byte-exact** on boss-fight snapshots 192126 (full=8
  bars) / 192140 (less=5 bars): render_frame band 0/640. Committed golden
  `tests/fixtures/object_render_boss_meter.json` + `test_object_render_boss_meter_byte_exact`.
- **Text/font renderer `9886`** — **RECOVERED** from the ASM (`pre2/recovered/text.py:draw_string`):
  the menu/title/score/tally text drawer. **VERIFY PENDING** — every snapshot is captured
  *after* the draw (the font segment `[0x2875]` + shade base + VGA state are gone), so there's
  no oracle; needs a snapshot taken *during* a text-screen draw. Not wired live.
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
