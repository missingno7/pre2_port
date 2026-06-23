# Faithful visual layer — consolidation plan (audit + target architecture)

The faithful renderer composes a *gameplay frame* well, but the recovered visual logic is still
spread across recovered leaves, bridge readers, checkpoints, probes, and a separate semantic model —
and transitions / scene changes are **not yet part of the live faithful flow** (observed: the
`--faithful` path renders gameplay but not the fades/iris/scene switches between frames). The end
state must be **one coherent faithful visual system**, not "scattered hooks + a separate frame
composer + duplicated transition logic". This document audits what exists and proposes the
consolidation. **Rule: do not duplicate recovered visual logic** — the live faithful pipeline must
*reuse* the recovered controller/leaf functions the checkpoints already verify, never reimplement them.

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
- **Phase A — FaithfulVisual scene dispatcher.** One entry `render_visual(GameVisualState) -> planes`
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

## Relationship to the other phases

This consolidation runs *alongside* the object-system recovery (state ownership): VisualControllers is
literally the visual half of "who creates the displayed state". The faithful renderer stays the live
diagnostic surface — do not patch renderer behavior to hide object-state errors. See
`object_system_island.md` (state producers), `scene_island.md` (scene leaves), `renderer_status.md`
(composition status).
