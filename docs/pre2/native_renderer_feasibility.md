# Native renderer with inter-frame interpolation — feasibility

**Verdict: feasible, and the recovered renderer is the right foundation.** We have effective
full control over the *gameplay frame* rendering, every input needed for position
interpolation is capturable each tick, and a midpoint frame has been rendered as a
proof-of-concept. This documents what we have, the evidence, and the work to build it.

## What we already have (full control over the gameplay frame)

- **`render_frame(RendererState)`** (`pre2/recovered/render_frame.py`): a VM-independent
  renderer that turns a plain-data state snapshot into the frame's pixels, proven byte-exact
  against the ASM (background ring buffer 0-div; `verify_render_frame.py`). It composes the
  recovered leaves: animated-grid + grid (tile background) → scroll → moving-sprite pass
  (`26FA`).
- **`RendererState`** (built by `pre2/bridge/render_state.py`, read-only) bundles **all the
  positions** an interpolator needs:
  - **Camera**: `camera_x`/`camera_y` (tiles) + `fine_scroll` (sub-tile px) + `scroll_src`.
  - **Moving sprites**: `object_sprites` — the active-sprite list, each with **world `x`/`y`
    (pixels)**, sprite id (graphic + flip + animation bits), and the per-id attributes +
    pixel-source banks.
  - Palette/fade + the tile map + attribute tables.

So the renderer is already decoupled from the VM: given a state snapshot, we draw the frame
without stepping the CPU.

## Evidence that interpolation works

Measured on gameplay snapshot 003317 over consecutive ticks:

1. **All moving gameplay sprites go through the `26FA` active list** that `render_frame`
   handles — 0 separate object-system blits in the frame. (The earlier residual diff was a
   *fixed-position* HUD element, which needs no interpolation.)
2. **Active-list slots are stable across ticks** — slot *i* holds the same object tick to
   tick (matched by base id `sprite_id & 0x1FFF`). This is the identity needed to pair an
   object between frames.
3. **Positions are smooth world coordinates** — e.g. slot 104 moved `x: 1090 → 1087 → 1084
   → 1081` (−3 px/tick).
4. **PoC**: pairing slots, lerping `(x,y)` to the midpoint `(1088,1408)`, and running the
   recovered `plan_sprite` produced the correct interpolated screen placement — the 2 px
   sub-byte offset lands in the blit `shift` field, so it is pixel-accurate, not byte-snapped.
   The only A↔B `dest_off` jump was the `0x2000` double-buffer page flip (irrelevant when we
   render to our own buffer).

## How the native interpolating renderer would work

Run game **logic** at the fixed tick (the VM's timer rate, ~25–35 Hz); render at display
rate (60 Hz+), drawing interpolated frames between the last two captured states:

```
each game tick (VM main loop, 1030:0214):
    capture S = read_renderer_state(mem)        # the post-update snapshot
    prev, cur = cur, S
each display frame (t in [0,1) between cur and prev):
    I = lerp_state(prev, cur, t)                # camera + per-slot sprite (x,y)
    render_frame(I, our_planes, our_dac)        # to our buffer, not VM VRAM
    present(our_planes)
```

`lerp_state`: interpolate `camera_x*16 + fine_scroll` (pixel-precise scroll) and each
matched slot's world `(x,y)`; carry id/animation/attrs from `cur` (animation steps at tick
rate — position-only interpolation, the standard approach).

## Work to build it (the gaps)

1. **Per-tick capture seam.** Hook the main-loop top (`1030:0214`) or `26FA` entry to call
   `read_renderer_state` once per tick and keep the previous snapshot. (Trivial — the bridge
   already exists.)
2. **State interpolation + object matching.** Match `object_sprites` by slot; lerp camera +
   `(x,y)`. Handle **spawn/despawn** (slot empty/filled on one side → no interp; pop at the
   `cur` position, or short fade). Handle **wrap/teleport** (large delta → snap, don't lerp).
3. **Render to our own framebuffer** and present it; ignore the VM's alternating display
   pages (the page flip is what made A/B `dest_off` differ).
4. **Background at sub-pixel scroll.** Two options: (a) reuse `scroll_copy` at the
   interpolated `fine_scroll` (works within a tile of slack), or (b) — cleaner for a true
   native renderer — render the tile background **directly from `TileMap` at any camera
   position** (full control, no ring-buffer dependency; the tile map + tables are in
   `RendererState`).
5. **HUD / fixed-screen elements.** The boss health bar (`0x135`, the no-camera `2784` path)
   and the score/lives are fixed-position — they need *no* interpolation, but `render_frame`
   must draw them. The `0x135` no-camera path is the one un-recovered branch in `26FA`
   (NEEDS REPRO — a frame with the boss bar active); other HUD digits are the separate
   text/font renderer `9886` (a distinct island). Until recovered, overlay them from the
   VM's own render at tick rate.

## Caveats

- **Animation vs position.** Sprite animation frames (the id's animation bits, `[0x6BC2]`)
  advance at the tick rate; interpolation is position-only. This is standard and looks
  smooth; interpolating animation is neither needed nor desirable.
- **Determinism preserved.** Logic still runs at the fixed tick, so gameplay/physics are
  unchanged and byte-reproducible; only presentation is upsampled. No input-latency change.
- **The object system stays the oracle for now.** Object positions come from the active list
  (the bridge reads them); we do not need to recover the object system (`65A0`/`8BFF`) to
  interpolate — only its *output positions*, which are already in `RendererState`.

## Bottom line

The recovered `render_frame(RendererState)` is exactly the drop-in seam this needs. Inter-
frame position interpolation is demonstrably feasible today for the gameplay frame; the
remaining work is a per-tick capture loop, the interpolation/matching layer, our own
present buffer, and finishing the fixed-screen HUD draws (boss bar `0x135` + text renderer).
