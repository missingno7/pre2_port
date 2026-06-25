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

## 0b. The enhanced contract: modern RGB/RGBA, planar only as an extractor

**Faithful preserves original pixels; enhanced preserves original meaning.** The enhanced runtime path is a
modern RGB/RGBA compositor — it must NOT drag the EGA planar model forward as its per-subframe hot path. The
recovered/faithful planar code (`render_frame`, `paint_sprite`) is used only as a **source-cadence
extractor/oracle**: generate the faithful base, extract the background and sprite textures once per source
frame, and certify `alpha=1` parity. Forbidden as the enhanced runtime path: per-subframe planar blits,
per-subframe deplanarize, depending on EGA planes/latches/byte-columns/pel-pan except as source data, or
re-calling the faithful rasterizer at display refresh.

**Audit (`pre2/probes/audit_enhanced_layers.py`) — all modern layers are exportable** at source cadence:
`background_rgb` via `render_frame(object_camera=None)` (clean bg-without-sprites; layer separation is clean),
`sprite_instances` from `GameFrameSnapshot` (id / image id / screen+world pos / draw order / flip / clip /
is_hud / mode / life), `sprite_rgba` for NORMAL sprites (mask+sprite → bg-independent texture; **all gameplay
witnesses are 100% NORMAL**), plus camera / animation-frame identity / faithful_frame. **Precise layer
flagged:** `OPAQUE`/`ERASE` sprites (flash/blink) are bg-dependent OR/mask blends — not standalone textures;
absent in steady gameplay; handled as per-sprite faithful passthrough (with a reported reason), never a silent
blend. First milestone scope: **gameplay moving NORMAL sprites only**; HUD/menu/CARTE/OLDIES/gameover/tally/
13h/fades/iris/curtains stay faithful passthrough.

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
4. **Minimal native sprite-compositing layer** — BUILT + PROVEN (compositor, not yet live-wired). The first
   real object-aware layer, modern RGB/RGBA: `pre2/enhanced/{frame_state,extract,compositor}.py`. Per source
   frame, `extract_enhanced_frame` renders the background (`object_camera=None`) + the faithful base via the
   verified `render_frame`, and lifts each NORMAL sprite into a **bg-independent RGBA texture** via the
   dual-buffer `paint_sprite` trick (paint onto all-0x00 + all-0xFF clean buffers; agree=opaque,
   differ=transparent). Per display subframe, `compose` blits sprites at `base_id`-matched interpolated
   positions over the cached background — pure RGB/RGBA, no planar. **Proven: `compose(alpha=1) == faithful`
   at 0 px** over spiders/player-death/gameplay/boss (`verify_enhanced_parity.py`); compositor logic tests in
   `tests/test_enhanced_compositor.py`. `OPAQUE`/`ERASE` (flash/blink, bg-dependent blends) are reported as
   `unsupported` and NOT faked; `is_hud` sprites (boss meter) are composited but not interpolated. HUD strip /
   menu / transitions stay faithful passthrough. This is **the first native enhanced sprite layer, NOT a full
   modern renderer**. NEXT: source-snapshot seam (prev/cur in FaithfulSession) + the native-refresh present
   loop to make the interpolation live.
5. (Later) widen coverage; smooth camera policy; etc. — all deferred.

## 2b. Live wiring (done)

`--video enhanced` is wired into the live viewer (gameplay-only object interpolation, faithful passthrough
everywhere else):
- **Source-snapshot seam**: `FaithfulSession` captures an `EnhancedFrameState` at each gameplay commit (6772,
  ~25 fps) and keeps `enh_prev`/`enh_cur` + wall-clock timestamps (`enh_clock`). Captured only on gameplay
  commits; scenes never hit 6772, so the snapshot goes stale → passthrough.
- **Present**: `EnhancedRenderer.present(now, faithful_frame)` computes `alpha = clamp((now − cur_time)/(cur
  − prev), 0, 1)` and returns `compose(cur, prev, alpha)`; it returns the faithful frame unchanged when
  interpolation is off (`--enhanced-no-interpolation`), no/stale/large-gap source snapshot, or non-gameplay.
  One source-frame of display latency (true interpolation, no extrapolation).
- **Native refresh** is achieved by presenting at the host refresh: run `--present-hz <monitor>` (e.g. 144 /
  240); the live loop presents at that cadence and `present()` interpolates each display frame. The game
  still advances at its own wall-clock rate (~25 fps source commits); enhanced never advances game state.
- **Diagnostics** (`status()` / `active_enhancements()`): backend, alpha, #interpolated sprites, #unsupported,
  and the faithful-passthrough reason.
- **Known v1 gaps (reported, not faked):** OPAQUE/ERASE flash/blink sprites are non-interpolated (unsupported);
  the particle (4B8E) / foreground-tile (3732) **effect layers** are not yet composited into the enhanced
  frame, so they are absent during interpolation (present in faithful; absent in steady witnesses). These are
  separate layers for a later pass — never invented, never pulled from the VM framebuffer.

## 3. Grounding rules (binding)

- No invented game state; no VM-framebuffer fallback; no generic frame-blend presented as object-aware; no
  faithful full-rasterizer per display subframe.
- "Object-aware" is only claimed when it actually uses the recovered `GameFrameSnapshot` object/camera state.
- Enhanced is NOT byte-exact (different presentation). It is *grounded*: positions/identity/graphics come
  from the byte-verified recovered leaves; only the pixel projection is new. Faithful stays the byte baseline.
- Verification: interpolation **math** + endpoints (alpha=0→prev, alpha=1→cur) are unit-testable headlessly;
  **smoothness** is a live-`--view` eyeball check (no headless high-refresh witness). Source cadence is
  measured (`measure_source_cadence.py`).
