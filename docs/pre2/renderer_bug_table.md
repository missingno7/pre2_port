# Faithful-renderer bug table (2026-06-24)

Built from the now-correct `--faithful-verify` signal (the 6772 frame-boundary `GameVisualState`
capture). At the boundary the camera/scroll false-positive is gone, so remaining diffs are real. Swept
the gameplay/transition witness set; ranked by impact.

| # | Witness / repro | Scene kind | Diff size / pattern | Affected layer | Classification | Root-cause hypothesis | Required fix | Type | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | boss 192126/192140; gp 003317/010021 | GAMEPLAY | HUD Δ 92–250, cols **272–304** | HUD (BONUS box) | **renderer bug — missing leaf** | `draw_hud` omitted the collected B/O/N/U/S letters (glyphs 0x0C–0x10, ASM 46AD), gated by `[0x6CA7]`/flash `[0x6C00]` | recover the 46AD loop into `draw_hud` (same `blit_hud_glyph` leaf) | renderer bug | **FIXED — HUD Δ=0 all witnesses (8604ba1)** |
| 2 | gp 185902 Δ58, boss_192140 Δ332 (frame-dependent: 0 on non-blink frames) | GAMEPLAY | viewport ≤~330, a few sprite/boss-meter px | moving sprites | **state-feed timing (object blink)** | the object pass mutates each record's blink/life `[+0x11]` (gated `[0x6BD5]&3`) AS it draws, so the 6772 capture (post-pass) is one blink-phase off for a sprite that toggled this frame | optional: capture object records at the object-pass ENTRY (pre-mutation) instead of 6772; **NOT a renderer-completion blocker** (≤1% viewport, only on blink frames, imperceptible) | state-feed bug (minor) | documented |
| 3 | menu modeselect/menuredraw; map mapscroll | SCENE (0Dh) | full frame (raises `FaithfulVisualGap`) | scene compose | **scene gap — bg rebuild is the hard sub-piece** | TEXT (4 `draw_string` @9920..99A7), palette, cursor located. BUT the BACKGROUND is a **0x2000 circular-page window of an infinitely-scrolling master pattern** (seg `[0x2875]`), panned by the CRTC (`display_start=0x0397`, `scroll_x=0xFE74` on the witness). RE-TRACED (2026-06-24): the producer is **`scroll_blit_column` @965A — `mov ds,[0x2875]` confirmed the pattern source** (planar: per plane the src reads stride 0x4F over 200 rows, di stride 0x27, page wrap 0x1FFF) — but it fills only **ONE new column per 8-px boundary** (`test dl,7;jne`); the BULK of the page is maintained by **`scroll_shift_frame`'s 4-plane self-copy @9804** (shifts the buffer to follow the camera). So the menu page is a **STATEFUL scroll buffer** (like the gameplay scroll-ring) — replaying `scroll_blit_column` alone reproduces ~1% (the self-copy history is lost). `render_scene` takes rebuilt planes (NOT a VRAM copy). | The leaves (`scroll_blit_column`/`scroll_shift_frame`) are already VERIFIED at their call sites (checkpoints/present.py). The mirror needs a NEW from-scratch **`build_menu_background(pattern[0x2875], scroll_x, scroll_y, page)`** = direct pattern sampling into the 0x2000 circular page (analogous to gameplay's `build_background_ring`), accounting for the planar 0x4F→0x28 stride remap + the scroll offset + CRTC pan. Then `SceneState` reader (bg + text_runs + palette + cursor) → `render_scene` → `render_visual`; verify at the menu commit boundary. | scene gap (multi-step) | OPEN — **needs `build_menu_background` (direct-sampling rebuild)**, a focused recovery; the per-column leaf is verified, the from-scratch composition is the work. Text/palette/cursor located. |
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
