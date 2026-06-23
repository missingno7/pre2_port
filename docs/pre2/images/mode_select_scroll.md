# Mode-select scrolling-background bug (visual)

The beginner/expert mode-select screen scrolls its background by **CRTC display-start panning +
page-flip double-buffering** (the `~1030:9600` present routine), *not* the gameplay scroll engine
(camera/ring/scroll-source are all zero here).

`display_start = (X/8 + Y·0x28) & 0x1FFF`, where `Y = [0xb19f]` is the scroll counter. The
`& 0x1FFF` (`and bh,0x1f`) means the screen treats the display as a **`0x2000`-byte (~205-row)
circular buffer** — the background is meant to **wrap** (repeat) at the page boundary.

**Bug:** only page 0 (`0x0`–`0x2000`) has the pattern + text; pages 1+ are empty. Our VM reads the
EGA display **linearly** past `0x2000` into the empty page, so the bottom goes blank and the text
scrolls off. The screen needs the scanline read to **wrap at `0x2000`**.

## The buffer (top → bottom)

The full plane buffer: the pattern + `MODE` / `BEGINNER` / `EXPERT` text fill rows 0–~205, then
it's empty. The 200-row display window pans down through this.

![buffer strip](modesel_buffer_strip.png)

## With `0x2000` wrap (intended) vs linear (our VM)

Left = scanline read **wrapping at `0x2000`** (the bottom wraps to the pattern top → seamless
repeat, text stays). Right = **linear** read (our VM): blank bottom, text scrolling away.

| wrap at 0x2000 (intended) | linear (our VM — the bug) |
|---|---|
| ![wrap](modesel_wrap2000.png) | ![linear](modesel_linear.png) |

## Fix (applied)

`scripts/sdl_view.render_planar_rgb` now wraps the scanline read at `0x2000` for single-page
screens — detected by *display-start in page 0 AND no plane content beyond `0x2000`*. Verified
across all snapshots: only the 3 menu/title screens are flagged; every gameplay snapshot has
23k–45k bytes past `0x2000` (the scroll ring) so it keeps the full `0x10000` wrap, unchanged.

Result (the menu with the fix):

![fixed](modesel_fixed.png)

Resolved: the "BEGINNER" **R** is *not* a render or port bug. The menu redraws every text run
each frame, and snapshot `075918` froze the framebuffer **mid-redraw of the R** — its lower six
rows (vrows 141–146, cols 27–30) have **no text-plane bits at all** (P2=P3=0, only the background
cavemen). So the R is genuinely half-written in memory and `render_planar_rgb` shows it faithfully.
Not a wrap/placement/font issue; nothing to fix in the viewer. (Confirmed by dumping the plane
bits at the R cell.) **Root cause confirmed from the present logic** (`1030:9613..9639`,
recovered in `pre2/recovered/present.py`): the mode-select present sets `page_draw = display_start`
— i.e. the text is drawn into the page **currently on screen** (there is no hidden back-buffer for
the menu text). So a per-frame redraw is genuinely observable mid-glyph; the half-R is the ASM
drawing the visible page, not a port artifact.

## How the screen is written (original) — scroll speed + text drift

The mode-select runs a **self-contained, vsync-paced loop** at `1030:97A8` (no game-frame/timer
gate). One iteration:

* `97A8–97C9` — CRTC **pan**: `display_start = (scroll_y·0x28 + scroll_x>>3) & 0x1FFF`, then the
  page flip (`present_pan_flip`). `scroll_x` (`[0xB19D]`) decreases steadily (horizontal roll);
  `scroll_y` (`[0xB19F]`) **oscillates sinusoidally** (the vertical "up/down" bounce).
* `97D6 → 9900` — the **per-frame vsync wait**.
* `97F8 → [0xB1AE]` — the scroll updater (advances `scroll_x`/`scroll_y`, i.e. the bounce phase).
* `9812–9817` — horizontal **scroll-blit** (`scroll_blit_column`, refill the newly-exposed column).
* `9819–9885` — the vertical bounce, rendered by **physically shifting the whole A000 framebuffer**
  up/down by the per-frame `scroll_y` delta (`movsb` self-copy, 200 rows × |delta|).
* `987E` — `jmp 97A8`.

### Scroll runs ~1.9× faster than DOSBox — an emulation timing detail, not a game bug

The vsync wait `9900` is a **sloppy half-wait**: *poll `0x3DA` until the retrace bit is **set***
(not a clean not-set→set edge). It paces the loop to the emulated vertical retrace, which we model
at **70 Hz with the bit asserted ~28 % of each frame** (`dos_re/dos.py`, `phase >= 0.72`). Because
it's a half-wait, the loop's effective rate is sensitive to two emulation parameters — the
retrace-bit **duty cycle** and the VM's **instruction throughput vs real time** — and both differ
from DOSBox, so the menu advances more steps/second here (user-measured ≈17 vs 9 vertical reversals
in 15 s). The scroll *geometry* is correct; only its *speed* depends on the retrace model. **Not an
original-game oversight** — the game just uses a timing-sensitive half-vsync-wait.

**Tuning knobs (live `--view`, opt-in, defaults unchanged):**
* `--retrace-pulse F` — fraction of each refresh the retrace status bit reads active
  (`dos.vga_retrace_active_fraction`, default `0.28` = legacy). A realistic narrow pulse
  (~`0.05`–`0.08`) gates the half-wait to **one frame per 70 Hz retrace** → correct menu speed.
  This is the actual cure (the game is vsync-gated, not CPU-bound).
* `--cpu-hz N` — an era-style instruction-rate ceiling (0 = unlimited). A safety throttle for
  *ungated* busy-loops + overall feel; not the menu cure. Our "instruction" ≠ a real CPU cycle,
  so tune by eye, not by a 386/486 cycle number.
* `--present-hz` (default **30**) — the display rate. The on-screen "low FPS" is this cap (plus
  render cost); raise to `60`/`70` for a smoother present.

Recommended combo to match DOSBox: `--present-hz 70 --retrace-pulse 0.06` (then tune the pulse).

### Text drift/jump — an original-game limitation (present in DOSBox too)

The vertical bounce is drawn by bulk-shifting the **entire framebuffer** (`9819–9885`) by the
`scroll_y` delta — and the **text is baked into that same framebuffer**, so it is dragged along with
the background, then re-stamped each frame by `draw_string`. The redraw can't perfectly cancel the
shift every frame, so the text wobbles — worst at the bounce's **direction changes** (largest
delta). There is no separate text layer; that's the original's limitation, which is why the wobble
also appears in DOSBox. **Enhanced-renderer fix:** composite text as a separate overlay *on top of*
the scrolled background (never baked in) — the recovered `SceneState` already separates background
(`SceneImage`) from text (`TextRun` list), so `render_scene` can draw the steady background then
overlay the text → rock-steady. A true-colour enhanced renderer also drops the framebuffer-shift
entirely (scroll becomes a source offset), removing the whole class of artifact.
