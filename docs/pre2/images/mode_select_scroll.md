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

**Question for the DOSBox oracle:** does the left (wrap) image match how this screen looks in
DOSBox? If so, the fix is in `dos_re` (key the EGA display read to wrap at the page for this
screen's config), without touching gameplay (which uses the full 64 KB plane + scroll ring).
