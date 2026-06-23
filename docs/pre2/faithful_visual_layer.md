# Faithful visual layer — consolidation plan (audit + target architecture)

The faithful renderer composes a *gameplay frame* well, but the recovered visual logic is still
spread across recovered leaves, bridge readers, checkpoints, probes, and a separate semantic model —
and transitions / scene changes are **not yet part of the live faithful flow** (observed: the
`--faithful` path renders gameplay but not the fades/iris/scene switches between frames). The end
state must be **one coherent faithful visual system**, not "scattered hooks + a separate frame
composer + duplicated transition logic". This document audits what exists and proposes the
consolidation. **Rule: do not duplicate recovered visual logic** — the live faithful pipeline must
*reuse* the recovered controller/leaf functions the checkpoints already verify, never reimplement them.

## ★ CURRENT PLAN & STATUS (2026-06-24) — read this first

This is the authoritative summary; the older audit sections below are kept for provenance but where
they conflict with this section, **this section wins**.

### The architecture (one leaf, many adapters — bidirectional convergence)

Every visual behavior has **ONE recovered implementation** (a pure fn in `pre2/recovered/`) with
multiple thin **adapters**, never a second copy:

```
                   pre2/recovered/<leaf>     ← the ONE implementation
                         ▲   ▲   ▲   ▲
      runtime hook ──────┘   │   │   └────── later enhanced projection
  (checkpoints/, ASM-skip)   │   └────────── frame-boundary faithful mirror
                             │               (render_visual / render_frame)
       checkpoint/probe verifier (oracle diff at the ASM RET)
```

`FaithfulVisual` (`recovered/faithful_visual.render_visual`) is the **umbrella OVER the leaves**, not a
second renderer. Convergence is **bidirectional**:
- **bottom-up:** ASM hook → verified recovered leaf → FaithfulVisual reuses it.
- **top-down:** if FaithfulVisual uses a leaf that lacks a checkpoint at its ASM call site, push it
  DOWN — locate the routine, add a verifier, diff vs the oracle, then both hook and mirror call the
  same leaf. (Applied to the HUD this session — see below.)

### Leaf-grounding map — every gameplay/transition leaf is now grounded

| Leaf | ASM | Runtime hook | Verify checkpoint | FaithfulVisual uses it |
|---|---|---|---|---|
| draw_grid / tile-row | 35A1 / 348D | ✓ skip | ✓ | ✓ (mirror via ring rebuild) |
| scroll_copy | 3A27 | ✓ skip | ✓ | ✓ |
| sprite blit | 3B69 | ✓ skip | ✓ | ✓ |
| object/sprite pass | 26FA | ✓ skip | ✓ | ✓ |
| anim cycle advance | 367D | passthrough (shadow) | ✓ | ✓ (bridge-read) |
| camera shake apply | 4C30 | passthrough (shadow) | ✓ | ✓ (row_factor) |
| palette fade | 6772 | ✓ skip | ✓ | DAC carries it (see deferred) |
| iris compose | 31F4 | ✓ skip | ✓ | ✓ (IRIS path) |
| **HUD draw** | **45B8** | — (verify-only; ASM draws) | **✓ NEW (`checkpoints/hud.py`)** | ✓ (`draw_hud`) |
| panel-flip / **curtain** | 3054 | passthrough (vsync pacing) | ✓ (final copy) | covered by render_frame (sub-frame) |
| draw_string | 9886 | ✓ skip | ✓ | scene leaf pending |
| menu scroll_blit / shift | 965A / 9804 | ✓ skip | ✓ | scene leaf pending |

### Resolved this session (2026-06-24)
- **Curtain CLOSED** (no faithful leaf needed). `3054` is the per-frame page-flip; the center-out strip
  reveal is **entirely sub-frame** (within one call, ~10 vsyncs). At the 6772 boundary the flip is
  complete, so the committed front page is always a whole frame that `render_frame` reproduces (proven:
  partially-revealed boundary `disp_black=17%` → mirror Δ=0%). Live reveal = the `frame_panel_copy`
  passthrough's vsync timing; `panel_copy` is the verified oracle (now `completed_pairs`-capable for an
  optional sub-frame mirror). Fixed the stale `panel_copy` docstring (`[0x2DD8]`/`[0x2DD6]`, strip copy
  `309B` — was wrongly `[0x2DD4]`/`[0x2DD2]`).
- **HUD grounded (bug-table #6 CLOSED)** — added verify-only `checkpoints/hud.py` at the dynamic-HUD ret
  (45AB); diffs recovered `draw_hud` glyph cells vs the ASM page `[0x2DD8]` (fired 53×, 0 divergences).
  Grounds `draw_hud` + `effective_bonus_mask`. Pruned the superseded `probes/verify_hud_layout.py`.
- **De-duplication:** the page-retarget dance (was copy-pasted in 3 sites) → one bridge helper
  `render_state.retarget_page`. The BONUS flash-parity DECISION moved OUT of the bridge into the leaf
  `recovered/hud.effective_bonus_mask` (bridge = state-extraction only).

### The ONLY remaining faithful-visual gaps (need RECOVERY, not just an adapter)
1. **SCENE leaf** (menu / map / loading / tally / game-over, mode 0Dh) — `render_visual` raises
   `FaithfulVisualGap`. Text/palette/cursor located + `draw_string`/`scroll_blit` leaves runtime-replaced,
   but the **background is a history-dependent scroll buffer** (`scroll_shift_frame` self-copy) — a
   from-scratch page↔pattern rebuild reaches only ~11%. **BLOCKED** on the buffer invariant or a
   scroll-replay (see `renderer_bug_table.md` #3). Do NOT guess a second theory.
2. **IMAGE leaf** (intro / title, mode 13h) — the displayed image is **not a direct `.SQZ` decode**;
   source unidentified (`renderer_bug_table.md` #4). Needs the source found before a `render_image` leaf.

### Deferred (NOT gaps — explicitly out of scope until their trigger)
- **Runtime *replacement* of HUD** (45B8 mode-2): the draw is incremental + dual-page + caches
  `[0x6CA0..0x6CA7]` → low gain; verify-only checkpoint is sufficient grounding.
- **Promote anim/shake shadows to mode-2** — proven 0-divergence; a clean next step, not required.
- **`GameFrameSnapshot` → `GameVisualState` convergence (Phase C)** — real overlap, but it serves the
  *enhanced-interp* master, not the byte-exact master; merging now would couple the verifier to the
  interpolator. Defer.
- **Whole-block collapse** (one `FaithfulVisual.render` hook over the main loop 0214-0270) — a
  state-ownership milestone, explicitly NOT the renderer-done bar.

---

## Audit — every visual island, classified

### 1. Frame composition (compose one frame's pixels — the gameplay slice is DONE byte-exact)
| Module | Role |
|---|---|
| `recovered/render_frame.py` | the gameplay-frame composer + seam (`render_frame(RendererState)`) |
| `recovered/frame_renderer.py` | background ring rebuild / draw_grid / scroll_copy / animated-grid |
| `recovered/object_render.py` | moving sprites + boss-meter `0x135` (plan_frame → paint_sprite) |
| `recovered/object_draw.py`, `renderer.py` | object draw primitive (`653D`) + sprite/bg blit |
| `recovered/hud.py` | status-bar chrome + dynamic overlay (`draw_status_bar`/`draw_hud`) |
| `recovered/sprite_decode.py`, `sprite_classify.py` | sprite-sheet decode + transparency class |
| (palette application = the DAC the frame is shown through) |

### 2. Visual state controllers (own + EVOLVE persistent visual state over time)
| Module (recovered pure fn) | State it owns | Live status |
|---|---|---|
| `recovered/transition.py` `fade_palette` | palette fade (PaletteState) | applied in render_frame DAC stage; **evolution bridge-READ** |
| `recovered/transition.py` iris (`build_scaled_columns`/`clear_span`) | iris/scale transition | recovered but **NOT called by render_frame** (RendererState.iris carried, ignored) |
| `recovered/animation.py` `advance_animation` | animated-tile cycle | **shadow-verified live** (checkpoint), but pipeline bridge-READs the evolved `[0x6BC2]` |
| `recovered/camera_shake.py` `apply_camera_shake` | shake → row_factor | **shadow-verified live** (checkpoint), pipeline bridge-READs `[0x6BF8]` |
| (scene mode / scene switching) | which scene is on screen | **NOT recovered** — `is_gameplay_frame` is a heuristic gate, not a scene-state read |

### 3. Scene renderers (per visual mode)
| Module | Mode | Status |
|---|---|---|
| `recovered/render_frame.py` | gameplay | **DONE** (byte-exact, live) |
| `recovered/scene.py` (`render_scene`/`SceneState`) | intro/menu/map/loading/tally | **DRAFTED** seam; leaves partial (`draw_string` etc.), verify-pending |
| `recovered/text.py` (`draw_string`) | menu/title/tally text | recovered, verify-pending |
| `recovered/present.py`, `bridge/present.py` | scene present (mode-select/map scroll pan) | partial |

### 4. Hook/checkpoint scaffolding (prove equivalence; should collapse into controllers)
`checkpoints/{palette,transition,animation,camera_shake,object_render,frame,blit,present,text,...}.py`
— verify-mode oracles. **These are correct as scaffolding** but must stay thin wrappers over the
SAME recovered fns (no second implementation). Probes `pre2/probes/verify_*.py` stay as proof harnesses.

### Cross-cutting: the SEMANTIC model + capture
`recovered/render_model.py` (`GameFrameSnapshot` = CameraState + Sprite/TileDrawCmd + PaletteState +
TransitionCmd + AnimationState + CameraShakeState + HudState + HudChromeAsset), built by
`render_snapshot.build_frame_snapshot(RendererState)`, captured by `bridge/frame_capture.py`,
interpolated by `render_interp.py`, presented by `enhanced/present.py`. This is the ENHANCED-side model.

## The real problems (why it feels scattered)

1. **Two parallel visual-state representations.** The live faithful path is `RendererState →
   render_frame` (machine-ish, byte-exact). The semantic `GameFrameSnapshot` is built *separately*
   from the same `RendererState` for the enhanced/interp path. Two truths that can drift.
2. **Controllers proven but not orchestrated.** `advance_animation`/`apply_camera_shake`/`fade_palette`
   are recovered + shadow-verified, but the live pipeline READS the ASM-evolved values via the bridge
   instead of RUNNING the controllers. The controller logic lives in checkpoints + as pure fns — not
   as the live source of the visual state.
3. **Transitions/scenes are outside the faithful flow.** render_frame ignores the iris; scene-to-scene
   fades and scene switching aren't orchestrated; the `--faithful` viewer handles only gameplay and
   falls back to ASM for everything else. So there is a faithful frame *composer*, not yet a faithful
   visual *layer* (whole visual behavior over time).

## Target architecture

```
VM  / (later) recovered game + scene logic
  └─> VisualControllers.evolve(prev_visual_state, inputs)      # RUN the recovered controllers
         (advance_animation, apply_camera_shake, fade step, iris step, scene-mode)  ← the SAME fns the checkpoints verify
  └─> GameVisualState                                          # ONE typed visual state
         scene_kind + { gameplay: RendererState/GameFrameSnapshot | scene: SceneState }
         + shared effect state (palette / transition / shake / animation)
  └─> FaithfulVisual.render(GameVisualState) -> planes         # scene dispatch, byte-exact
         gameplay → render_frame ; transition → iris/fade overlay ; scene → render_scene
  └─> (later) EnhancedRenderer(GameVisualState)                # modern output, same state, NOT byte-diffed
```

- `render_frame()` stays the **gameplay frame composer** (do NOT dump scenes/transitions into it).
- `FaithfulVisual` is the new **orchestrator above it** that makes transitions + scenes part of the flow.
- `VisualControllers` is where the recovered controller fns become the **live owner** of the evolving
  visual state (ties directly into the state-ownership phase — same shadow-proven fns).
- `GameVisualState` converges `RendererState` (gameplay machine input) and `GameFrameSnapshot`
  (semantic) so there is one truth; the enhanced renderer consumes it.

## Consolidation plan (phased, non-breaking, reuse-not-reimplement)

- **Phase 0 — this audit + rule.** (done)
- **Phase A — FaithfulVisual scene dispatcher.** **STARTED (2026-06-23, commit cd11d64) — iris first.**
  `scene_kind` discovery: PRE2 has **no global scene enum** (across labeled scene snapshots no DGROUP
  byte enumerates gameplay/menu/map/tally; the game dispatches by routine), so it is DERIVED
  (`bridge/scene_state.py`): IRIS if `[0x2DD0]!=0`, IMAGE if video 13h, GAMEPLAY if a level is loaded
  with a non-origin camera `[0x2DE4]/[0x2DE6]` (the old `[0x6BC2]` gate was too loose — menus share
  that range), else SCENE. `recovered/faithful_visual.py:render_visual(kind, rs, planes, iris)` routes
  GAMEPLAY→`render_frame`, IRIS→`render_frame`+`compose_iris`, IMAGE/SCENE→fall back to the VM frame
  (leaves not recovered yet). The iris compose is the single shared `recovered/transition.compose_iris`
  (the checkpoint `_run` now calls it — one impl). Verified: routing PASS on all 7 scene witnesses;
  faithful iris vs ASM = only the moving-sprite phase residual. `play.py --faithful` now routes via the
  dispatcher (iris live; menu/map/intro correctly fall back). REMAINING in Phase A: recover the IMAGE
  (intro/title) + SCENE (menu/map/loading/tally/game-over) leaves so they render faithfully too; and
  the GAMEPLAY-vs-SCENE camera-origin edge (level-start frame) is a documented minor fallback.
- ~~**Phase A — FaithfulVisual scene dispatcher.**~~ One entry `render_visual(GameVisualState) -> planes`
  that dispatches by `scene_kind`: gameplay → `render_frame`; transition → apply the recovered iris
  (`build_scaled_columns`/`clear_span`) + `fade_palette` over the composed frame; scene →
  `render_scene`. Wire the live viewer to it (replace the `is_gameplay_frame` heuristic with a
  recovered `scene_kind` read — depends on locating the scene-mode var, the scene-island work).
  Result: transitions + scenes enter the faithful flow; honest fallback only for truly-unrecovered scenes.
- **Phase B — VisualControllers (live ownership).** A module that evolves the persistent visual state
  by CALLING the recovered controller fns (the ones the checkpoints already prove == ASM), replacing
  the bridge READ of evolved `[0x6BC2]`/`[0x6BF8]`/fade/iris. No new logic — orchestration only.
- **Phase C — state convergence.** Fold `RendererState` (gameplay machine input) + `GameFrameSnapshot`
  (semantic) into `GameVisualState`; derive one from the other instead of maintaining both. Enhanced
  renderer consumes `GameVisualState`.
- **Phase D — collapse scaffolding.** Checkpoints become thin verify wrappers over the controller fns;
  probes remain proof harnesses. The hook surface shrinks as controllers own the state (coastline).

## The non-duplication rule (enforced)

Every visual behavior has exactly ONE recovered implementation (a pure fn in `recovered/`). The
checkpoint (verify), the VisualControllers (live evolve), and the FaithfulVisual (compose) all call
THAT fn. If a behavior is recovered as a checkpoint today, the faithful pipeline absorbs it by calling
the same fn — it must never grow a second copy that drifts. `render_model`/`render_snapshot` stay the
semantic projection of the same state, not a parallel implementation.

## Completion audit (2026-06-23) — what is missing from the faithful visual body, by bucket

Buckets: **1** renderer/composer can't draw it · **2** visual state not exported · **3** recovered
controller not orchestrated (live flow reads ASM-evolved value instead of running the recovered fn) ·
**4** scene/transition dispatcher incomplete · **5** gameplay/object producer gap.

| Behavior | Evidence (current) | Bucket | Required state | Current source | Required fix | Blocks faithful? | Needs object recovery? |
|---|---|---|---|---|---|---|---|
| Gameplay bg/tiles/parallax/scroll | byte-exact live (verify_live_faithful) | — done | RendererState | bridge + render_frame | — | **No** | No |
| Moving sprites | byte-exact (boss frame 0/28160) | — done | object_sprites/attrs/banks | bridge + object pass | — | **No** | No |
| HUD chrome + overlay | 0/3680 (test_hud_chrome) | — done | HudState + HudChromeAsset | ALLFONTS asset + render_frame | — | **No** | No |
| Boss meter | 0/640 (192126/192140) | — done | 0x135 sprites | object pass | — | **No** | No |
| Palette application (static) | deplanarize via live DAC | — done | DAC | live vga_palette | — | **No** | No |
| **Palette FADES** | shows via live DAC; `fade_palette` NOT run (live_render `dac=None`) | **3** | PaletteState (exported) | bridge-fed DAC | run `fade_palette` in a VisualController | No (displays) | No |
| **Camera shake** | shows via `row_factor` [0x6BF8] bridge read; `apply_camera_shake` not run | **3** | CameraShakeState (exported) | bridge-fed `row_factor` | run `apply_camera_shake` | No (displays) | No |
| **Animation advance** | shows via `anim_xlat` bridge read; `advance_animation` not run | **3** | AnimStep (exported) | bridge-fed `[0x6BC2]` slice | run `advance_animation` | No (displays) | No |
| **Iris transition** | `render_frame` carries `s.iris` but never calls the iris leaf | **4** (+3 leaf) | IrisState (exported) | bridge-fed | transition dispatcher calls `build_scaled_columns`/`clear_span` over the frame | **YES** | No |
| **Scene-change fades/transitions** | faithful path is gameplay-only; not composed | **4** | TransitionCmd/PaletteState | ASM fallback | FaithfulVisual scene+transition dispatch | **YES** | No |
| **Scene switching (mode)** | `is_gameplay_frame` is a heuristic; no scene-mode var | **2 + 4** | `scene_kind` (NOT exported) | heuristic | locate the scene-mode var → `scene_kind` | **YES** | No |
| **Menu/map/intro/loading/tally/game-over** | not rendered faithfully (ASM fallback); `scene.py` drafted, verify-pending | **4** (+1/2 leaves) | SceneState (partial) | ASM | recover+verify scene leaves + dispatcher | **YES** | No (scene logic ≠ objects) |
| Particles / effects (`4b8e`) | NEEDS-REPRO; own-blit vs active-list unknown | **5 or 1 (OPEN)** | particle state (unknown) | ASM | get a witness → classify | Unknown | Maybe |
| Blink residual on fast motion | ≤5px; object pass mutates `[+0x11]` mid-draw → live read off-phase | **5** (state timing) | pre-mutation records | bridge-fed at present instant | own the object update (deterministic phase) | No (cosmetic; boss frame is 0) | Yes |
| Object state generally (positions/ids) | bridge-fed reads; renders correctly | **5** (on ownership) | object_sprites/attrs | bridge-fed | recover object update (state ownership) | No (displays) | Yes |

### Conclusion — what actually blocks the faithful visual body

- **Bucket 1 (renderer can't draw): essentially none** for gameplay — composition is complete. (Only
  particles *might* be bucket 1, pending a witness.)
- **Bucket 3 (fade / shake / animation controllers): NOT visual gaps** — they all DISPLAY correctly via
  bridge-fed reads. They are *orchestration/cleanliness* (run the recovered fn instead of reading the
  evolved value) = the consolidation plan, not visual completeness.
- **Bucket 5 (object producer): does NOT block faithful visuals** — the object state is bridge-fed and
  renders fine; the only visual symptom is the ≤5px blink residual (cosmetic). The "500" popup / object
  recovery is about *owning* the state, not making the picture appear.
- **Bucket 4 is the ONLY thing blocking faithful visual completion:** (a) **transition dispatch** (iris +
  scene-change fades — the recovered leaves exist, they are just not orchestrated/composed), and (b)
  **scene rendering** (menu / map / intro / loading / tally / game-over — needs a recovered `scene_kind`
  + the scene leaves recovered & verified).

**So: finishing the faithful visual body does NOT require object-system recovery.** The critical path is
the scene/transition dispatcher (Phase A) + locating the scene-mode variable + recovering the scene
leaves. The object/popup work (bucket 5) stays correctly queued — it is the *state-ownership* track, not
the *visual-completion* track. Bucket 3 folds in alongside as the controllers become the live owners.

## Island fusion — the merge target, not a parallel system (2026-06-23)

The faithful renderer was bootstrapped somewhat as a parallel composer; it must now become the
**merge target** that the existing rendering hooks/controllers/leaves collapse INTO — one large
`FaithfulVisual` island, not "a renderer plus scattered visual hooks". Audit answers:

**Q1 — Which recovered hooks/controllers absorb into FaithfulVisual?** The four persistent
visual-state controllers — `fade_palette` (palette fade), the iris (`compose_iris`),
`advance_animation` (anim cycle), `apply_camera_shake` (shake apply). They exist as recovered fns +
verify checkpoints, but the LIVE faithful path still READS their ASM-evolved values via the bridge
(`[0x6BC2]`/`[0x6BF8]`/fade/iris) instead of RUNNING them. Absorb them by a `VisualControllers.evolve`
that runs the SAME fns to produce the evolving visual state (the iris is already absorbed into
`render_visual`).

**Q2 — Which functions are duplicated or parallel?** The controller LOGIC is already single-impl
(`fade_palette`/`advance_animation`/`apply_camera_shake`/`compose_iris` each recovered once; the
checkpoint and composer call the same fn — `compose_iris` was the last inline copy, now removed). The
remaining PARALLELISM is structural: the **live faithful pipeline** (`render_visual → render_frame`)
vs the **semantic pipeline** (`render_snapshot → GameFrameSnapshot → render_interp/enhanced`) — two
pipelines off the same `RendererState`. Converge: both consume one `GameVisualState`.

**Q3 — Which state models overlap?** `RendererState` (gameplay frame input) and `GameFrameSnapshot`
(semantic projection, built FROM `RendererState`) overlap in palette/shake/anim/hud/camera — LAYERED,
not independent, but those fields have NO single owner. Converge into one canonical
`GameVisualState = { scene_kind, gameplay: RendererState, scene: SceneState|None, fx: VisualFxState }`
where `VisualFxState` (palette/iris/shake/anim) is the ONE owner; `GameFrameSnapshot` becomes the
SEMANTIC PROJECTION of `GameVisualState` for the enhanced renderer (no duplicated ownership).

**Q4 — Which hook scaffolding can collapse?** As `VisualControllers.evolve` runs the controllers live
(owning the visual state), the bridge READ paths in `read_renderer_state` (`[0x6BC2]`/`[0x6BF8]`/fade/
iris reads) collapse — the controllers PRODUCE that state. The checkpoints shrink to thin verify-only
diffs over the same recovered fns.

**Q5 — Which pieces are only proof scaffolding?** `pre2/probes/verify_*.py` (lockstep proof harnesses)
and the `pre2/checkpoints/*` in verify mode (the ASM oracle). These do NOT collapse into FaithfulVisual
— they VERIFY it, and persist while the ASM is the oracle.

**Q6 — Which pieces become canonical visual-controller modules?** NEW
`pre2/recovered/visual_controllers.py` = `VisualControllers.evolve(prev_fx, inputs)` running the
recovered controller leaves → next `VisualFxState` (the ONE place visual state evolves).
`recovered/faithful_visual.py` = the dispatcher (island root). `recovered/render_model.py` =
`GameVisualState` (canonical) + `GameFrameSnapshot` (its projection).

**Q7 — Final module boundary for the large faithful visual island:**

```
pre2/recovered/   == the FaithfulVisual island (pure, byte-verifiable, ONE coherent subsystem)
  faithful_visual.py     SceneKind + render_visual dispatcher           [ISLAND ROOT / merge target]
  visual_controllers.py  VisualControllers.evolve (runs the controller leaves)        [NEW]
  render_model.py        GameVisualState (canonical) + GameFrameSnapshot (projection)
  render_frame.py        gameplay frame composer                         (leaf)
  frame_renderer / object_render / object_draw / renderer / hud / sprite_*  composition leaves
  transition.py          fade_palette · compose_iris · clear_span        (transition leaves)
  scene.py · text.py     menu/map/intro/loading/tally scene leaves
pre2/bridge/      == the ONLY place that reads VM memory -> feeds GameVisualState
  scene_state · render_state · palette · transition · ...
pre2/checkpoints/ == verify-only oracle (thin wrappers over the recovered fns; shrinks over time)
pre2/probes/      == lockstep proof harnesses
```

The boundary: `recovered/` IS the island (one faithful visual subsystem); `bridge/` is the VM↔state
seam; `checkpoints/`+`probes/` are verification scaffolding that prove the island, not part of it.

**Fusion phases (non-breaking):** A — `render_visual` dispatcher (gameplay+iris done; scene/image
leaves next; the loud no-fallback gap forces their completion). B — `VisualControllers.evolve`: run
the recovered controllers, converging the bridge READ paths into controller RUNS (visual-state
ownership). C — `GameVisualState` convergence (RendererState/SceneState as slices + VisualFxState as
the single fx owner; GameFrameSnapshot as its projection). D — collapse the checkpoints to verify-only.
Each phase keeps the lockstep-vs-ASM oracle and the one-impl rule.

## Runtime integration audit (2026-06-23) — mode-1 (viewer mirror) vs mode-2 (ASM actually replaced)

Two senses of "faithful render", which must be distinguished: **mode 1 (viewer mirror)** = the VM
runs, the bridge reads state, `render_visual`/`render_frame` re-composes a clean frame for display
(proof/diagnostic — does NOT remove the ASM). **mode 2 (runtime replacement)** = the game reaches an
ASM render call, the `@registry.replace` hook runs the recovered leaf, writes the exact side effects,
and **skips the ASM body** (`cpu.s.ip = pop/EXIT`) — the old call is gone from the live path.

**Key finding: almost the entire renderer is ALREADY mode-2.** Auditing each hook's *live* (non-verify)
branch:

| Routine | CS:IP | Recovered leaf | Live behaviour | Class |
|---|---|---|---|---|
| draw_tile_row | 3476 | `draw_tile_row` | skips ASM (`pop`) | **RUNTIME-REPLACED** |
| draw_grid | 35A1 | `draw_grid` | skips ASM | **RUNTIME-REPLACED** |
| scroll_copy | 3A27 | `scroll_copy` | skips ASM | **RUNTIME-REPLACED** |
| object/sprite pass | 26FA | `plan_frame`/`paint_sprite` | skips ASM | **RUNTIME-REPLACED** |
| sprite blit | 2C00 | `blit_sprite` | skips ASM | **RUNTIME-REPLACED** |
| palette fade | 6772 | `fade_palette` | skips ASM | **RUNTIME-REPLACED** |
| iris | 31F4 | `compose_iris` | skips ASM (→32B0) | **RUNTIME-REPLACED** |
| draw_string (text) | 9886 | `draw_string` | skips ASM | **RUNTIME-REPLACED** |
| menu bg scroll-blit | 965A | `scroll_blit_column` | skips ASM | **RUNTIME-REPLACED** |
| menu framebuffer scroll | 9804 | `scroll_shift_frame` | skips ASM | **RUNTIME-REPLACED** |
| sprite_decode / sqz / audio | … | recovered | skips ASM | **RUNTIME-REPLACED** |
| **panel_copy / curtain** | 3054 | `panel_copy` | **passthrough (ASM runs)** | **VERIFY-ONLY — can't replace (vsync-paced reveal timing IS the effect; a pure hook would hang the det-clock)** |
| **anim advance** | 367D | `advance_animation` | **passthrough** | **VERIFY-ONLY shadow (PROVEN) — deliberately not authoritative (state-ownership "keep ASM oracle"); can be promoted to mode-2** |
| **camera-shake apply** | 4C30 | `apply_camera_shake` | **passthrough** | **VERIFY-ONLY shadow (PROVEN) — can be promoted to mode-2** |

So the user's worry — "ASM renderer still runs and the viewer re-renders afterward" — is mostly NOT the
case: the recovered leaves ARE the live render path (the ASM bodies are skipped). The `--faithful`
viewer is an *additional* mode-1 whole-frame re-compose (`render_frame`/`render_visual`) that shares
those same leaves — useful as the clean-framebuffer proof, redundant with the (already recovered)
hybrid VRAM.

### Answers to the audit questions
- **Already runtime-replaced:** grid/tile_row/scroll_copy, object+sprite blit, palette fade, iris,
  draw_string, menu bg scroll, decode/sqz/audio. (≈ the whole renderer.)
- **Only viewer-level re-rendering:** `render_frame`'s whole-frame orchestration + its `rebuild`
  path (`build_background_ring`) are mode-1 only (the runtime uses the per-leaf hooks + incremental
  `draw_grid`). Same leaves, different orchestration — not a duplicate impl.
- **Verify-only scaffolding (ASM still runs):** curtain `panel_copy` (3054), `anim_advance` (367D),
  `camera_shake_apply` (4C30).
- **Old ASM that still executes despite a recovered impl:** those same three.
- **Replacements blocked by unmodelled side effects:** (a) the curtain's per-step vsync PACING (its
  pixels are recoverable as a partial `panel_copy`, but the timing is presentation, not state);
  (b) the **whole-render-block collapse** into one `FaithfulVisual.render` hook — blocked because the
  main loop (0214-0270) interleaves the render leaf-calls WITH game logic, so one hook can't replace
  the block without recovering that interleaved logic ⇒ **converges with the state-ownership track**.
- **Need lifting from VGA/page semantics first:** the curtain (page-copy + vsync → a frame-clock /
  per-step surface), the page-flip/displayed-page choice (presentation detail — just fixed for the
  viewer), and the scene leaves (menu present → a `SceneState` surface).

### Runtime-integration plan (collapse toward one FaithfulVisual island)
1. **Promote the two PROVEN verify-only shadows to mode-2** (`advance_animation`, `apply_camera_shake`):
   flip their live branch from passthrough to skip-ASM + write the contract. They are already
   shadow-verified 0-divergence — this removes two more ASM calls from the live path (the cleanest
   next mode-1→mode-2 step) while the checkpoint stays as the verify oracle.
2. **Curtain:** model the per-step reveal as a partial `panel_copy(step)` (the pixels), keep the
   vsync PACING as the VM's (or the enhanced renderer's own clock) — it can become a faithful
   *rendered* effect (viewer + enhanced) even if the live hook stays passthrough for timing.
3. **Scene leaves (menu/map/intro/…):** recover + hook each (most of the menu is already mode-2 —
   bg scroll + draw_string; what's missing is the `SceneState` assembly + a clean-FB scene compose).
4. **The collapse to one `FaithfulVisual.render` hook is a SEPARATE, FUTURE milestone (#3 below) — NOT
   the renderer-done bar.** It is gated on recovering the interleaved game logic (state-ownership).
   Until then, the per-leaf mode-2 hooks ARE a valid canonical render path; `render_frame`/`render_visual`
   is the whole-frame mirror it will later converge onto. One-impl rule holds throughout (checkpoint +
   runtime hook + mirror call the same leaf). See "Renderer completion — definition + checklist" below.

## Renderer completion — definition + checklist (NOT gated on whole-block collapse)

Three SEPARATE milestones — do not conflate them:

1. **Faithful visual completion (the renderer-done bar).** Every visual mode + effect is rendered by a
   recovered `FaithfulVisual` leaf/controller from explicit visual state, verified against the oracle,
   with NO ASM-VRAM fallback. The VM may still PRODUCE and SCHEDULE the visual state — but every visual
   OPERATION is a recovered leaf, and the final frame is verified by the clean mirror.
2. **Runtime leaf replacement.** Each render/transition/text/palette/scene leaf is hooked at its
   original call site and skips the ASM body where safe (mode-2). Mostly already done (see the audit).
3. **Whole-block collapse (FUTURE — a state-ownership milestone, NOT required for "renderer done").**
   Replacing the entire interleaved main-loop render block (0214-0270) with one `FaithfulVisual.render`
   call. Blocked by the interleaved game logic. Until the game tick / main-loop orchestration is
   recovered, the **per-leaf mode-2 hooks ARE a valid canonical rendering path** — the original loop
   may schedule the visual ops, but the ops themselves are recovered leaves.

**Renderer boundary:** *the VM may produce + schedule visual state; every visual operation is a recovered
FaithfulVisual leaf/controller; the final frame is verified by the clean FaithfulVisual mirror.*

### Finite renderer-completion checklist + status (2026-06-23)

| Item | Status |
|---|---|
| Gameplay composition verified | **DONE** (byte-exact offline at the 2DF9 boundary; bg+sprites+HUD+boss) |
| Palette / DAC behaviour verified | **DONE** (`fade_palette` verified + runtime-replaced) |
| Transitions: **iris** modeled + verified | **DONE** (`compose_iris`, verified, runtime-replaced) |
| Transitions: **fade** | **DONE** (palette fade; DAC on the live palette) |
| Transitions: **curtain** (`panel_copy`) modeled + verified | **DONE (2026-06-24)** — sub-frame page-flip; mirror reproduces every committed boundary frame (Δ=0); `panel_copy` verified oracle + `frame_panel_copy` passthrough |
| Text / present leaves verified | **PARTIAL** — `draw_string` + menu present runtime-replaced; verify-pending a mid-draw witness |
| Scene leaves: menu/map | **OPEN** — menu located (bg present + 4 text runs + highlight); SceneState reader + verify pending |
| Scene leaves: intro/title **IMAGE** (13h) | **OPEN** — not recovered (fails loud) |
| Scene leaves: loading / tally / game-over | **OPEN** |
| No silent ASM-VRAM fallback | **DONE** (`render_visual` raises `FaithfulVisualGap`) |
| All visual call sites classified (replaced / lifted / frame-boundary-verified / verify-only+blocker) | **DONE** (runtime-integration audit table above) |
| Whole-frame mirror matches oracle across camera movement | **DONE (2026-06-24)** — frame-boundary `GameVisualState` capture at 6772 (`game_visual_state.py`); cave witness 231731 Δ=0, worst gameplay boundary Δ≤58 (blink residual) |
| Whole-frame mirror matches oracle across SCENE changes | **OPEN** — only because the SCENE/IMAGE leaves aren't recovered (mirror raises `FaithfulVisualGap`, no silent fallback) |

So "renderer done" = the remaining OPEN rows closed: the **scene (menu/map) + image (intro/title)
leaves recovered+verified**. Curtain, the frame-boundary mirror, and the HUD are now DONE. The whole-block
collapse is explicitly OUT of this definition (it follows later, with state ownership).

## One-implementation audit (2026-06-24) — one recovered leaf, many adapters

Target: each visual behavior = ONE recovered fn, used by the runtime-hook adapter, the FaithfulVisual
mirror (`render_frame`/`render_visual`), and the verify checkpoint/probe — differing only in adapters.

| Leaf (CS:IP) | Shared recovered fn | Runtime hook (ASM skipped?) | FaithfulVisual mirror | Verify | One-impl status / missing adapter |
|---|---|---|---|---|---|
| draw_tile_row (3476) | `draw_tile_row` | `frame_tile_row` ✓ skip | yes (via grid + ring rebuild) | ✓ | **ONE-IMPL** ✓ |
| scroll_copy (3A27) | `scroll_copy` | `frame_scroll_copy` ✓ skip | `render_frame` ✓ | ✓ | **ONE-IMPL** ✓ |
| object/sprite (26FA) | `plan_frame`/`paint_sprite` | `object_render` ✓ skip | `render_frame` object pass ✓ | ✓ | **ONE-IMPL** ✓ |
| sprite blit (2C00) | `blit_sprite` | `sprite_blit` ✓ skip | via `paint_sprite` ✓ | ✓ | **ONE-IMPL** ✓ |
| iris (31F4) | `compose_iris` | `iris_transition` ✓ skip | `render_visual` IRIS ✓ | ✓ | **ONE-IMPL** ✓ |
| scene present (965A/9804) | `scroll_blit_column`/`scroll_shift_frame` | `scroll_blit`/`scroll_shift` ✓ skip | (scene leaf not wired) | ✓ | runtime+verify ✓; **mirror pending** (scene) |
| draw_string (9886) | `draw_string` | `draw_string_hook` ✓ skip | (scene leaf not wired) | ✓ | runtime+verify ✓; **mirror pending** (scene) |
| **draw_grid (35A1)** | `draw_grid` (incremental) | `frame_grid` ✓ skip | mirror uses **`build_background_ring`** (full rebuild), NOT `draw_grid` | ✓ | **two ORCHESTRATIONS** of the same `draw_tile_row` leaf — incremental (runtime, needs ring history) vs full rebuild (mirror, clean FB has no history). Legit, but the grid-walk logic is expressed twice; lift the shared walk if it drifts. |
| **palette fade (6772)** | `fade_palette` | `palette_fade` ✓ skip | mirror runs it only if `dac` passed — `live_render` passes `dac=None` → mirror uses the LIVE DAC, NOT `fade_palette` | ✓ | **mirror does not run the leaf** (DAC carries the fade) → run `fade_palette` in a VisualController for a single owner |
| **HUD (45B8)** | `draw_hud`/`draw_status_bar`/`blit_hud_glyph` + `effective_bonus_mask` | verify-only (ASM draws; mode-2 deferred) | `render_frame` rebuild ✓ | **`checkpoints/hud.py` ✓ (live oracle diff, NEW 2026-06-24) + golden `test_hud_chrome`** | **ONE-IMPL ✓** — leaf grounded by a registered checkpoint; runtime *replacement* deferred (low gain) |
| **curtain (3054)** | `panel_copy` (now `completed_pairs`-capable) | `frame_panel_copy` **PASSTHROUGH** (vsync pacing IS the effect) | covered by `render_frame` (reveal is sub-frame; boundary Δ=0) | ✓ (final planes) | **ONE-IMPL ✓** — sub-frame effect; no separate mirror leaf needed (see status section) |
| scene render (render_scene) | `render_scene` (partial) | n/a | not wired (SCENE gap) | n/a | **mirror + SceneState reader missing** |
| image (13h intro/title) | — | — | gap | — | **not recovered** |

**Conclusion (updated 2026-06-24):** the one-impl rule now HOLDS for **all gameplay + transition leaves**
including the HUD (grounded by `checkpoints/hud.py`) and the curtain (sub-frame, no separate leaf). The
only remaining one-impl items are cleanups, not violations: (1) **palette fade** mirror uses the live DAC,
not `fade_palette` — run the leaf in a VisualController (deferred, displays correctly); (2) **draw_grid vs
build_background_ring** are two orchestrations of the shared `draw_tile_row` (legit: incremental-with-ring-
history vs clean-FB rebuild — keep the walk shared if it drifts). The remaining true RECOVERY work is the
**scene + image leaves** (`render_visual` fails loud for them). Frame-boundary `render_game_visual_state`
follows the rule (reuses `render_visual` → the same leaves; no second copy).

## Relationship to the other phases

This consolidation runs *alongside* the object-system recovery (state ownership): VisualControllers is
literally the visual half of "who creates the displayed state". The faithful renderer stays the live
diagnostic surface — do not patch renderer behavior to hide object-state errors. See
`object_system_island.md` (state producers), `scene_island.md` (scene leaves), `renderer_status.md`
(composition status).
