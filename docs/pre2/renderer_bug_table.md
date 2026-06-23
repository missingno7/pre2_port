# Faithful-renderer bug table (2026-06-24)

Built from the now-correct `--faithful-verify` signal (the 6772 frame-boundary `GameVisualState`
capture). At the boundary the camera/scroll false-positive is gone, so remaining diffs are real. Swept
the gameplay/transition witness set; ranked by impact.

| # | Witness / repro | Scene kind | Diff size / pattern | Affected layer | Classification | Root-cause hypothesis | Required fix | Type | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | boss 192126/192140; gp 003317/010021 | GAMEPLAY | HUD Δ 92–250, cols **272–304** | HUD (BONUS box) | **renderer bug — missing leaf** | `draw_hud` omitted the collected B/O/N/U/S letters (glyphs 0x0C–0x10, ASM 46AD), gated by `[0x6CA7]`/flash `[0x6C00]` | recover the 46AD loop into `draw_hud` (same `blit_hud_glyph` leaf) | renderer bug | **FIXED — HUD Δ=0 all witnesses (8604ba1)** |
| 2 | gp 185902 Δ58, boss_192140 Δ332 (frame-dependent: 0 on non-blink frames) | GAMEPLAY | viewport ≤~330, a few sprite/boss-meter px | moving sprites | **state-feed timing (object blink)** | the object pass mutates each record's blink/life `[+0x11]` (gated `[0x6BD5]&3`) AS it draws, so the 6772 capture (post-pass) is one blink-phase off for a sprite that toggled this frame | optional: capture object records at the object-pass ENTRY (pre-mutation) instead of 6772; **NOT a renderer-completion blocker** (≤1% viewport, only on blink frames, imperceptible) | state-feed bug (minor) | documented |
| 3 | menu modeselect/menuredraw; map mapscroll | SCENE (0Dh) | full frame (raises `FaithfulVisualGap`) | scene compose | **scene gap — bg rebuild is the hard sub-piece** | TEXT (4 `draw_string` @9920..99A7), palette, cursor located. BUT the BACKGROUND is a **0x2000 circular-page window of an infinitely-scrolling master pattern** (seg `[0x2875]`), panned by the CRTC (`display_start=0x0397`, `scroll_x=0xFE74` on the witness). Both a flat and a windowed `scroll_blit_column(pattern[0x2875], scroll_x)` replay reproduce only ~1% of the page (72/7079 non-zero bytes) — so **the menu-background mechanism is NOT what the `scroll_blit_column` model assumes** (wrong source/offset, or the bg is built by `scroll_shift_frame` self-copy / a static tiled image / a different routine). The actual menu-present composition at `1030:98E2` must be re-traced before a clean rebuild is possible. `render_scene` takes rebuilt planes (NOT a VRAM copy). | **FIRST: re-trace the actual menu-bg composition at 98E2** (what fills the 0x2000 page — is the caveman-heads pattern a static tiled image, a `scroll_shift_frame` self-copy, or a different blit; find the true source + mapping). THEN: `build_menu_background`, `SceneState` reader, `render_scene` wiring, verify at the commit boundary. | scene gap (multi-step) | OPEN — **bg mechanism unrecovered** (my scroll_blit model is wrong; needs a focused re-trace of 98E2). Text/palette/cursor located. |
| 4 | intro 163804; title 163923 | IMAGE (13h) | full frame (gap) | image | **scene gap** | no recovered linear-image (mode 13h) leaf | recover a `render_image` leaf + bridge image inputs | scene gap | OPEN |
| 5 | cave-enter curtain | transition | (no witness yet) | transition | **transition/controller gap** | `panel_copy` final copy recovered, but the per-step vsync-paced strip reveal is not modeled in the mirror | recover `panel_copy_partial(step)`; needs a **mid-3054 witness** (cs:ip 3054..309A) | transition gap | OPEN (witness needed) |
| 6 | HUD runtime path | GAMEPLAY | no live divergence | HUD | **one-impl/adapter gap (not a visual bug)** | the HUD leaf has no runtime hook — the ASM still draws the HUD live; recovered only in mirror + golden test | add a 45B8 runtime hook (or document as mirror+golden-verified) | adapter gap | OPEN (low priority) |
| 7 | palette fade (021225) | GAMEPLAY | 0 (DAC carries the fade) | palette | **controller ownership (not a visual bug)** | the mirror uses the live DAC, not `fade_palette` | run `fade_palette` in a VisualController so the mirror owns the evolution | ownership cleanup | OPEN (cleanup) |
| 8 | particles `4b8e` | — | no active witness | effects | **unknown (no witness)** | unclear if any direct-blit particle is uncovered by the object/sprite pass | get a particle witness (`[0x7DE6]!=-1`); classify | unknown | OPEN (witness needed) |

## Summary
After the frame-boundary fix + the HUD BONUS fix, the **gameplay + iris visual output is byte-exact at
the commit boundary** (viewport HUD Δ=0; the only viewport residual is the negligible object-blink
phase, #2). The remaining faithful-renderer work is all **scene/transition leaves** (#3 SCENE, #4 IMAGE,
#5 curtain) + non-visual cleanup (#6 HUD adapter, #7 palette ownership) + the open #8 particle question.

None of the remaining items is a gameplay/object-producer gap — the visual STATE exists and is exported;
what's missing is recovered LEAVES for the non-gameplay scenes/transitions. So the renderer-completion
track stays independent of state ownership (per the milestone separation).

Highest-impact next: a **SCENE leaf** (menu/map, #3) — most witnesses, reuses the already-runtime-replaced
`scroll_blit`/`draw_string` leaves; then the curtain (#5, needs a witness) and IMAGE (#4).
