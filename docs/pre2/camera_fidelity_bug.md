# Camera-movement / cave-enter faithful-verify mismatch — diagnosis (2026-06-23)

Witness: `artifacts/snapshot_pre2_20260623_231731` (mid cave-enter curtain). With the on-screen-page
verify fix (`96ebdbb`), `--video-verify` reports a large viewport mismatch during fast camera
movement / cave enter-exit. This is the evidence-based diagnosis (no fallback, no tolerance).

## Evidence

| Test | Result |
|---|---|
| Per-row diff (render_frame vs displayed page) | **uniform ~100/160 every row, 0 rows match**; HUD (176-199) = 0 |
| Diff image | base parallax (sky/mountain) + HUD **match**; the **entire tile layer is wrong** |
| Stability (drive 6 frames, no input) | mismatch **stable ~21000** (not phase noise) — state stuck `cam=(118,37) prev=(118,37) scroll_src=0x41E4 col_ring=0x12` |
| State anomaly | **`dest_page=0x0000` but displayed page = `0x2000`** (the live state targets the BACK buffer; the viewer shows the FRONT) |
| Rebuilt ring vs live VRAM ring (0x3F40..0x5E00) | **2507/31488 = 8%** — `build_background_ring` is ~correct |
| `scroll_copy(LIVE ring, live scroll params) → displayed page` | **22189/28160 = 79% mismatch** — the correct ring + live scroll params do NOT reproduce the displayed page |

## Classification: **B (state-feed / snapshot timing) + C (page/display-start) + D (scroll lifecycle)** — NOT A (composition)

Ruled OUT:
- **A (renderer composition):** the ring is 92% correct and the base/HUD are byte-exact; feeding the
  *correct* ring to `scroll_copy` still fails — so the leaf math is not the bug.
- **Simple timing/phase:** the mismatch is stable across frames, not noise.

The bug is that the **scroll-copy parameters + page** read from live state describe the frame the engine
is currently **building** (`dest_page=0x0000`), while the viewer displays/verifies the **front** page
(`0x2000`) — a *different* frame, composed with the *previous* frame's `scroll_src`/`col_ring`/`dest`.
The double-buffer page-flip + the per-frame scroll-ring advance mean an **ad-hoc live read of
`RendererState` is internally inconsistent with the displayed page**. (In steady gameplay the two
frames' scroll state is nearly identical, so the offline proofs at 185902 etc. passed; during cave
enter-exit / fast scroll they diverge, and the curtain freezes the displayed page out of phase.)

This is exactly the anticipated failure mode: *GameVisualState needs a frame-boundary snapshot instead
of ad-hoc live reads.*

## Required fix (explicit, no symptom-masking)

The faithful mirror must reproduce the page **the state describes**, captured at the **same frame
boundary** — not mix the live (back-buffer) scroll state with the displayed (front) page:

1. **Frame-boundary snapshot.** Capture `(scroll_src, col_ring, row_ring, fine_scroll, dest_page,
   camera, prev_camera, row_factor, …)` at the instant a frame is committed/flipped (the page-flip /
   `scroll_copy` RET), and reproduce **that** page from **that** snapshot. The project already has the
   scaffolding (`bridge/frame_capture.py` `FrameCapture` / the GameVisualState idea) — this lifts the
   ad-hoc `read_renderer_state` into a frame-boundary capture. This is the canonical fix.
2. **Until then, verify only at a frame-complete boundary** (the offline `verify_live_faithful` samples
   at the object-pass RET `2DF9`, where state↔page are phase-aligned, and gets byte-exact). The live
   `--video-verify` samples at arbitrary wall-clock instants, so it WILL show this mismatch during
   movement — which is correct (it must not hide it); the title Δ is a true "state↔page out of phase"
   signal, not a renderer error.

EXPLICIT consequence to document (per the constraint): the **viewer-level mirror** (live ad-hoc read)
and the **runtime-replaced output** (the hybrid leaves drawing the actual page) diverge during page
transitions *because the mirror reads the wrong frame's state*, not because the leaves are wrong. The
fix is the frame-boundary snapshot, not a viewer tolerance or an ASM fallback.

## Frame-boundary capture — BUILT (2026-06-23)

The commit boundary is **`1030:6772`** (palette-fade entry — the LAST op in the per-frame main loop,
AFTER the page flip). There `ega_display_start` is the just-committed frame and the scroll/camera state
has not yet advanced, so `render_frame(state)@display_start == display_start`.

`pre2/bridge/game_visual_state.py`: `GameVisualState` (scene_kind + RendererState with dest_page =
committed page + iris) + `capture_game_visual_state(mem, dos, display_page, game_root)` (capture ONLY
at 6772) + `render_game_visual_state(gvs)` (reuses `render_visual` → the same recovered leaves). This is
the canonical verification substrate.

PROOF (`pre2/probes/verify_frame_boundary.py`, driven gameplay, pure-ASM oracle): at the 6772 boundary
the capture reproduces the displayed page **Δ≤58 (blink-phase only)**; a read ~600 instr OFF the
boundary mismatches the displayed page **Δ=675–912** — so the boundary is necessary and sufficient
(no tolerance, no fallback). The live `--video-verify` mismatch during movement was a TRUE
state↔page out-of-phase signal; capturing at 6772 resolves it.

VIEWER WIRED (2026-06-24, commit 462199e): `play.py --video faithful` now mirrors the 6772-boundary capture
(a hook wrapping the palette-fade hook at 6772 captures + renders + (with `--video-verify`) diffs vs
the displayed page; the viewer shows the latest capture for gameplay/iris). SCENE/IMAGE still fail loud
(`FaithfulVisualGap` diagnostic + console), never ASM VRAM. Regression `verify_frame_boundary` covers the
cave witness (231731 @6772 Δ=0). `render_game_visual_state` reuses `render_visual` → the same leaves
(one-impl). The cave-enter witness was NOT mid-curtain (both pages full; 44E4 is a vsync wait) — so this
fully resolves the camera-fidelity bug. The ACTUAL curtain (`panel_copy` per-step reveal) needs a
mid-3054 witness (cs:ip in 3054..309A during cave ENTER) — investigated next, along with the HUD runtime
adapter, SCENE/IMAGE leaves, and palette-controller ownership.
