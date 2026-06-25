# Enhanced renderer — design (source-cadence presentation)

> The enhanced renderer is a **native presentation layer**, not a second VM renderer and not a re-run of the
> faithful planar rasterizer per display frame. It consumes recovered game/render state produced at the
> game's **native update cadence**, keeps the previous/current snapshots, and presents at the **display
> refresh**, rendering selected layers natively and passing everything else through faithful.

## 0. The two clocks (the core correction)

PRE2's VGA retrace is 70 Hz, but the **game logic updates far less often**. Measured
(`pre2/probes/measure_source_cadence.py`, gameplay + injected movement):

| Source-frame commit (1030:6772) | value |
|---|---|
| **Source fps (emulated)** | **~25 fps** |
| Retrace cycles per source frame | **~2.8** (histogram: mostly 3, some 4) |
| ic per source frame | ~6000 |

So a *new gameplay frame* is produced ~25×/sec (every ~3 retraces), matching the `1C6F` "wait ~3 PIT ticks"
governor. The 70 Hz retrace and the page flip are **display/oracle timing**, NOT the enhanced renderer's
drawing model.

**What changes per source frame** (`measure_source_cadence.py` `characterize`, moving witnesses), i.e. what
the interpolation rides on:

| Witness | objects moved | max screen Δ/frame | animation step | fade |
|---|---|---|---|---|
| Spiders (active) | 119/119 frames (~4.4 objs) | 107 px (sprite motion) | ~25% of frames | 0 |
| Player-death | 57/119 | 9 px | ~25% | 0 |
| Gameplay + scroll | 85/119 | 175 px (camera scroll) | ~25% | 0 |
| Shake | 14/119 | 8 px | 0 | 0 |

Takeaways: in active play **objects move nearly every source frame** (so per-object interpolation is the real
win, not just camera), screen deltas are large enough to see stepping (up to ~107 px sprite / ~175 px with
scroll), the tile **animation cycle** steps ~1/4 of frames (interpolate as a discrete swap, do NOT lerp tile
ids), and **palette fades are transition-only** (0 during steady gameplay — fade projection is a separate,
infrequent concern). Live objects/frame ≈ 2–10.

**Consequence (feasibility).** The expensive faithful render is **9.26 ms** (full planar rebuild + sprite
pass + deplanarize). Re-running it per *display* subframe is infeasible (240 Hz → 2.2 cores). But it only has
to run per **source** frame: 25 × 9.26 ms ≈ **23% of one core**. The display subframes (the 3× / 6× / 10×
fill at 70 / 144 / 240 Hz) are **cheap interpolation** (~0.8 ms viewport ops + per-sprite RGB blits), never a
faithful re-rasterization. The earlier "object-aware interpolation is blocked" conclusion was wrong — it came
from re-rasterizing per display subframe; the fix is to re-render only per source frame.

## 1. Architecture

```
hybrid runtime  --(native update cadence ~25 fps)-->  FaithfulSession
    per source frame: capture grounded state (GameFrameSnapshot / RendererState / PaletteState / ...)
                      + the faithful base image
    keep prev + cur source snapshots
        |
        v
EnhancedRenderer.present(now, display_refresh)        # called at the DISPLAY refresh (70/144/240 Hz)
    alpha = (now - cur_source_time) / source_period   # interpolation fraction in [0,1)
    frame = faithful_base(cur)                         # the passthrough base
    frame = project_truecolor_fade(frame, cur.palette) # presentation effect (per source frame)
    frame = project_transition(frame, cur.iris/curtain)
    frame = interpolate_camera(frame, prev, cur, alpha) # cheap viewport shift (display subframe)
    frame = interpolate_objects(frame, prev, cur, alpha)# native sprite layer (display subframe)
    return frame                                        # unimplemented layers -> faithful passthrough
```

- **Source clock**: the game's ~25 fps commit (6772). Enhanced re-derives state + the faithful base here.
- **Display clock**: the host vsync. Enhanced presents here, interpolating between the last two source
  snapshots by `alpha`.
- **alpha**: wall-time fraction between source frames. `alpha→0` = prev positions, `alpha→1` = cur.

Enhanced never reads the VM framebuffer/`mem`, never advances game state, never re-runs the faithful planar
rasterizer per display subframe, and falls back to the faithful base for any layer it doesn't implement.

## 2. Layer roadmap (implement one at a time; each grounded, each falls back to faithful)

1. **Faithful base** — done (FaithfulSession; `--video enhanced` passthrough proven pixel-identical).
2. **Truecolor palette fade / iris / curtain projection** — presentation effects on the *source* frame. The
   fade keeps the recovered phase/curve/timing (`PaletteState`, the byte-verified `fade_palette`) but is
   projected at 24-bit precision instead of the 6-bit DAC steps. Needs the session to expose the **indexed**
   image + `PaletteState` (the faithful RGB has the fade baked at 6-bit, so it can't be post-corrected).
   Cheap (palette recompute, no per-subframe render). Falls back to faithful when no fade/transition active.
3. **Cheap camera / scroll interpolation** — at the display refresh, translate the viewport by the
   interpolated camera delta (grounded in `CameraState`; gameplay scroll + CARTE/menu pans). A viewport
   translation, **not** a cross-fade blend. Honest where the motion is camera-only; sprites still step at the
   source rate (camera-aware, not yet per-object).
4. **Minimal native sprite-compositing layer** — the first real object-aware layer: cache the background
   (from the faithful/recovered source frame), and per display subframe blit each sprite at its interpolated
   position (from `GameFrameSnapshot` via `interpolate_frame`, object identity by `base_id`) using native
   RGB/pygame sprite surfaces/masks built from the captured sprite graphics. HUD / menu / transitions stay
   faithful passthrough. This is **the first native enhanced sprite layer, NOT a full modern renderer**.
5. (Later) widen coverage; smooth camera policy; etc. — all deferred.

## 3. Grounding rules (binding)

- No invented game state; no VM-framebuffer fallback; no generic frame-blend presented as object-aware; no
  faithful full-rasterizer per display subframe.
- "Object-aware" is only claimed when it actually uses the recovered `GameFrameSnapshot` object/camera state.
- Enhanced is NOT byte-exact (different presentation). It is *grounded*: positions/identity/graphics come
  from the byte-verified recovered leaves; only the pixel projection is new. Faithful stays the byte baseline.
- Verification: interpolation **math** + endpoints (alpha=0→prev, alpha=1→cur) are unit-testable headlessly;
  **smoothness** is a live-`--view` eyeball check (no headless high-refresh witness). Source cadence is
  measured (`measure_source_cadence.py`).
