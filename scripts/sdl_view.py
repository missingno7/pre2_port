"""SDL/pygame display backend for the interactive DOS_RE viewer (``play.py``).

The old target used to render through Tk; profiling showed two stacked costs per
displayed frame that motivated moving to SDL:

  * ``render_*_ppm`` builds a *scaled* RGB byte string in a pure-Python pixel
    loop (~3.1 ms at scale 2), then it is written to a temp ``.ppm`` file and a
    brand-new ``tk.PhotoImage(file=...)`` is parsed back from disk every frame
    (~2.1 ms);  and
  * Tk's ``root.after(1, ...)`` repaint scheduling is ~15 ms-granular on Windows,
    so the emulator stalls in ``FrameSync.publish_and_wait`` waiting for the UI.

This backend instead:

  * decodes the video memory with vectorised NumPy at *native* 320x200 (so the
    Python pixel work is ~0.8 ms and independent of ``--scale``);
  * uploads it straight to an SDL surface (no temp file, no per-frame PhotoImage)
    and lets SDL scale it to the window; and
  * polls ``FrameSync`` directly from the pygame loop, so the present round-trip
    is ~1-2 ms instead of ~15 ms.

The decoders are pixel-identical to the reference ``render_*_ppm`` functions in
``render_frame.py`` (asserted by ``tests/test_render_rgb.py``); those PPM renderers
remain as the headless PNG-dump tool and as the decode oracle, while this module
is what the live viewer uses.

``play.py`` imports this module only when it actually launches the viewer, so the
core runtime, the PNG tool and the tests do not require ``pygame``.
"""
from __future__ import annotations

from queue import Empty
from typing import Callable

import numpy as np

from render_frame import (
    CGA_PALETTES,
    EGA_BYTES_PER_ROW,
    EGA_LEGACY_PLANE_STRIDE,
    EGA_PALETTE,
    EGA_SHADOW_BASE,
    EGA_PLANE_STRIDE,
    TANDY_BANK_STRIDE,
    TANDY_BYTES_PER_ROW,
    DEFAULT_VGA_PALETTE,
)

WIDTH, HEIGHT = 320, 200

_EGA_PAL = np.array(EGA_PALETTE, dtype=np.uint8)  # (16, 3)
_TEXT_MODES = {0, 1, 2, 3, 7}
_TEXT_PALETTE = [
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
]

_DOS_5X7_PATTERNS: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    '"': ("01010", "01010", "01010", "00000", "00000", "00000", "00000"),
    "#": ("01010", "11111", "01010", "01010", "11111", "01010", "01010"),
    "$": ("00100", "01111", "10100", "01110", "00101", "11110", "00100"),
    "%": ("11001", "11010", "00100", "01000", "01011", "10011", "00000"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "'": ("00100", "00100", "01000", "00000", "00000", "00000", "00000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "11100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    ";": ("00000", "01100", "01100", "00000", "01100", "00100", "01000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "@": ("01110", "10001", "10111", "10101", "10111", "10000", "01111"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00001", "00001", "00001", "00001", "10001", "10001", "01110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "\\": ("10000", "01000", "00100", "00010", "00001", "00000", "00000"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "^": ("00100", "01010", "10001", "00000", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "`": ("01000", "00100", "00010", "00000", "00000", "00000", "00000"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "~": ("00000", "00000", "01000", "10101", "00010", "00000", "00000"),
}

_TEXT_GLYPH_CACHE: dict[int, np.ndarray] = {}


def _bitmap_mask_for_code(ch: int) -> np.ndarray:
    """Return a crisp 8x16 bitmap mask for a BIOS text character.

    The F9 boss-key screen is a BIOS text mode screen, not an game graphics
    surface.  Rendering it through pygame's proportional outline font made the
    fake DOS directory listing look like scaled UI text.  A small ROM-like 5x7
    bitmap expanded into an 8x16 cell keeps text deterministic, monospace, and
    nearest-neighbour friendly.  CP437 box/extended glyphs fall back to '?' until
    we need them; the boss screen itself uses plain ASCII.
    """
    ch &= 0xFF
    cached = _TEXT_GLYPH_CACHE.get(ch)
    if cached is not None:
        return cached
    if 0x61 <= ch <= 0x7A:
        key = chr(ch - 0x20)
    elif 0x20 <= ch <= 0x7E:
        key = chr(ch)
    else:
        key = "?"
    rows = _DOS_5X7_PATTERNS.get(key, _DOS_5X7_PATTERNS["?"])
    mask = np.zeros((16, 8), dtype=bool)
    y0 = 1
    x0 = 1
    for src_y, row_bits in enumerate(rows):
        for src_x, bit in enumerate(row_bits):
            if bit == "1":
                mask[y0 + src_y * 2:y0 + src_y * 2 + 2, x0 + src_x] = True
    _TEXT_GLYPH_CACHE[ch] = mask
    return mask


def render_text_rgb(mem: bytes, mode: int, page: int = 0) -> np.ndarray:
    """Decode BIOS 80x25 text memory to a native 640x400 RGB image.

    This intentionally does not use host fonts.  The source screen is already a
    character-cell bitmap device, so using a deterministic bitmap mask avoids
    anti-aliased/proportional glyph artifacts and makes integer SDL scaling look
    much closer to a DOS text screen.
    """
    base = 0xB0000 if (mode & 0xFF) == 7 else 0xB8000
    page_off = (page & 0x07) * 0x1000
    cell_w, cell_h = 8, 16
    arr = np.zeros((25 * cell_h, 80 * cell_w, 3), dtype=np.uint8)
    mem_arr = np.frombuffer(mem, dtype=np.uint8)
    for row in range(25):
        y = row * cell_h
        for col in range(80):
            x = col * cell_w
            off = base + page_off + ((row * 80 + col) * 2)
            if off + 1 >= mem_arr.size:
                continue
            ch = int(mem_arr[off]) or 0x20
            attr = int(mem_arr[off + 1])
            fg = _TEXT_PALETTE[attr & 0x0F]
            bg = _TEXT_PALETTE[(attr >> 4) & 0x07]
            cell = arr[y:y + cell_h, x:x + cell_w]
            cell[:, :] = bg
            mask = _bitmap_mask_for_code(ch)
            cell[mask] = fg
    return arr


class PcSpeakerAudio:
    """Tiny SDL square-wave renderer for PIT channel 2 / port 61h events."""

    def __init__(self, pygame) -> None:
        self._pygame = pygame
        self._channel = None
        self._freq_key = 0
        self._cache = {}
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)
        init = pygame.mixer.get_init()
        if init is None:
            raise RuntimeError("pygame mixer did not initialize")
        self._rate = int(init[0])
        self._channels = int(init[2])

    def set(self, enabled: bool, freq: float) -> None:
        if not enabled or freq < 20.0:
            self.close()
            return
        key = max(20, min(20000, int(round(freq))))
        if key == self._freq_key and self._channel is not None:
            return
        self.close()
        self._channel = self._sound_for(key).play(loops=-1)
        self._freq_key = key

    def close(self) -> None:
        if self._channel is not None:
            self._channel.stop()
        self._channel = None
        self._freq_key = 0

    def _sound_for(self, freq: int):
        sound = self._cache.get(freq)
        if sound is not None:
            return sound
        rate = self._rate
        samples = max(rate // 50, int(rate / max(freq, 1)) * 2)
        t = np.arange(samples, dtype=np.float64)
        phase = (t * float(freq) / rate) % 1.0
        wave = np.where(phase < 0.5, 3500, -3500).astype(np.int16)
        if self._channels > 1:
            wave = np.repeat(wave[:, None], self._channels, axis=1)
        sound = self._pygame.sndarray.make_sound(wave)
        self._cache[freq] = sound
        return sound


class NukedAdlibAudio:
    """SDL streaming wrapper around the vendored optional ``nuked_opl3`` package.

    The VM already runs the original AdLib driver and forwards completed
    YM3812 register writes.  This class only turns that register stream into PCM.
    Keeping it in the viewer preserves headless determinism and lets tests run
    without compiling the CFFI extension.
    """

    def __init__(
        self,
        pygame,
        status: dict | None = None,
        *,
        enabled: bool = True,
        chunk_ms: float = 46.0,
    ) -> None:
        self._pygame = pygame
        self._status = status
        self._available = False
        self._chip = None
        self._channel = None
        self._rate = 44100
        self._channels = 1
        self._chunk_frames = 2048
        self._started = False
        self._underruns = 0
        self._last_status_underruns = 0
        if not enabled:
            return
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)
        init = pygame.mixer.get_init()
        if init is None:
            self._report("AdLib audio unavailable: pygame mixer did not initialize")
            return
        self._rate = int(init[0])
        self._channels = int(init[2])
        self._chunk_frames = max(512, int(round(self._rate * max(10.0, float(chunk_ms)) / 1000.0)))
        try:
            from nuked_opl3 import OPL3  # type: ignore

            self._chip = OPL3(sample_rate=self._rate)
        except Exception as exc:  # noqa: BLE001 - optional extension/import failure
            self._report(
                "AdLib register stream active, but vendored Nuked-OPL3 is not built/importable: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        self._available = True
        self._channel = pygame.mixer.Channel(1)
        self._report("AdLib audio: vendored Nuked-OPL3 backend active")

    def write(self, reg: int, value: int) -> None:
        if not self._available or self._chip is None:
            return
        self._chip.write(int(reg) & 0x1FF, int(value) & 0xFF)

    def pump(self) -> None:
        if not self._available or self._chip is None or self._channel is None:
            return
        # Pygame's mixer API is chunk-queue based rather than callback based.
        # Keep the current chunk plus one queued chunk filled.  If rendering or
        # the emulator thread briefly takes longer than one chunk, this avoids a
        # stop/start gap; if the channel did underrun, restart with a prequeued
        # follow-up chunk so the next interval is stable again.
        if not self._channel.get_busy():
            if self._started:
                self._underruns += 1
            self._channel.play(self._next_sound())
            self._channel.queue(self._next_sound())
            self._started = True
            self._report_underrun_status()
        elif self._channel.get_queue() is None:
            self._channel.queue(self._next_sound())

    def close(self) -> None:
        if self._channel is not None:
            self._channel.stop()

    def _report_underrun_status(self) -> None:
        if self._underruns == self._last_status_underruns:
            return
        self._last_status_underruns = self._underruns
        self._report(
            f"AdLib audio: vendored Nuked-OPL3 backend active, underruns={self._underruns}, "
            f"chunk={self._chunk_frames * 1000.0 / max(1, self._rate):.0f}ms"
        )

    def _next_sound(self):
        assert self._chip is not None
        pcm = self._chip.generate_mono(self._chunk_frames)
        if self._channels > 1:
            arr = np.frombuffer(pcm, dtype=np.int16)
            pcm = np.repeat(arr[:, None], self._channels, axis=1).astype(np.int16).tobytes()
        return self._pygame.mixer.Sound(buffer=pcm)

    def _report(self, text: str) -> None:
        if self._status is not None:
            self._status["text"] = text


def render_ega_rgb(mem: bytes, start_offset: int = 0, seg: int = 0xA000) -> np.ndarray:
    """Decode the EGA shadow planes to a native (200, 320, 3) RGB array.

    Mirrors ``render_frame.render_ega_ppm`` exactly, including its three accepted
    buffer layouts (distinguished by length): a tight view of just the four shadow
    planes (planes at offset 0, the layout the live viewer publishes), full runtime
    memory (planes at ``EGA_SHADOW_BASE``), or the legacy in-aperture layout for old
    byte snapshots.  Each byte is eight horizontal pixels (MSB first) and the colour
    index is one bit from each plane.  ``start_offset`` is the CRTC display-start
    byte offset (the original wraps it at 16 bits per row).
    """
    arr = np.frombuffer(mem, dtype=np.uint8)
    if arr.size == EGA_PLANE_STRIDE * 4:
        base, stride = 0, EGA_PLANE_STRIDE
    elif arr.size >= EGA_SHADOW_BASE + EGA_PLANE_STRIDE * 4:
        base, stride = EGA_SHADOW_BASE, EGA_PLANE_STRIDE
    else:
        base, stride = (seg & 0xFFFF) * 16, EGA_LEGACY_PLANE_STRIDE
    start = start_offset & 0xFFFF
    rowbase = (start + np.arange(HEIGHT) * EGA_BYTES_PER_ROW) & 0xFFFF
    off = (rowbase[:, None] + np.arange(EGA_BYTES_PER_ROW)[None, :]) & 0xFFFF  # (200,40)
    color = np.zeros((HEIGHT, EGA_BYTES_PER_ROW, 8), dtype=np.uint8)
    for plane in range(4):
        plane_bytes = arr[base + plane * stride + off]              # (200,40)
        bits = np.unpackbits(plane_bytes[..., None], axis=2)        # (200,40,8) MSB-first
        color |= bits << plane
    return _EGA_PAL[color.reshape(HEIGHT, WIDTH)]


def render_cga_rgb(mem: bytes, palette: str = "1h") -> np.ndarray:
    """Decode CGA B800h 320x200x4 to a native (200, 320, 3) RGB array.

    Mirrors ``render_frame.render_ppm``: interlaced layout
    ``offset = (y & 1)*0x2000 + (y >> 1)*80``; each byte is four pixels, two bits
    each, most-significant pixel first.
    """
    arr = np.frombuffer(mem, dtype=np.uint8)
    pal = np.array(CGA_PALETTES[palette], dtype=np.uint8)           # (4,3)
    base = 0xB8000
    y = np.arange(HEIGHT)
    rowbase = base + (y & 1) * 0x2000 + (y >> 1) * 80               # (200,)
    cols = arr[(rowbase[:, None] + np.arange(80)[None, :])]         # (200,80)
    idx = np.stack([(cols >> s) & 3 for s in (6, 4, 2, 0)], axis=2)  # (200,80,4)
    return pal[idx.reshape(HEIGHT, WIDTH)]


def render_tandy_rgb(mem: bytes) -> np.ndarray:
    """Decode Tandy/PCjr B800h 320x200x16 packed graphics to (200, 320, 3) RGB.

    Mirrors ``render_frame.render_tandy_ppm``: four 8 KiB banks,
    ``offset = (y & 3)*0x2000 + (y >> 2)*160 + x_byte``; each byte is two pixels,
    high nibble first.
    """
    arr = np.frombuffer(mem, dtype=np.uint8)
    base = 0xB8000
    y = np.arange(HEIGHT)
    rowbase = base + (y & 3) * TANDY_BANK_STRIDE + (y >> 2) * TANDY_BYTES_PER_ROW
    cols = arr[(rowbase[:, None] + np.arange(TANDY_BYTES_PER_ROW)[None, :])]  # (200,160)
    idx = np.stack([(cols >> 4) & 0x0F, cols & 0x0F], axis=2)                  # (200,160,2)
    return _EGA_PAL[idx.reshape(HEIGHT, WIDTH)]


def render_vga_rgb(mem: bytes, palette: list[tuple[int, int, int]] | None = None) -> np.ndarray:
    """Decode VGA mode 13h A000:0000 linear 320x200x8bpp to RGB."""
    arr = np.frombuffer(mem, dtype=np.uint8)
    pal = np.array(palette if palette is not None else DEFAULT_VGA_PALETTE, dtype=np.uint8)
    idx = arr[0xA0000:0xA0000 + WIDTH * HEIGHT].reshape(HEIGHT, WIDTH)
    return pal[idx]


def render_text_surface(pygame, mem: bytes, mode: int, page: int):
    """Render BIOS 80x25 colour/mono text memory to a pygame surface."""
    rgb = render_text_rgb(mem, mode, page)
    return pygame.image.frombuffer(rgb.tobytes(), (80 * 8, 25 * 16), "RGB")


# pygame key -> XT make scan code.  Letters/digits use pygame's lowercase names;
# the named keys cover the legacy DOS-game controls plus the
# usual editing/arrow keys, matching the Tk KEYSYM_SCAN table in play.py.
def _build_pygame_scan() -> dict[int, int]:
    import pygame

    name_scan: dict[str, int] = {
        "escape": 0x01, "-": 0x0C, "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
        "[": 0x1A, "]": 0x1B, "return": 0x1C, "enter": 0x1C,
        "left ctrl": 0x1D, "right ctrl": 0x1D, ";": 0x27, "'": 0x28,
        "`": 0x29, "left shift": 0x2A, "\\": 0x2B, ",": 0x33, ".": 0x34,
        "/": 0x35, "right shift": 0x36, "left alt": 0x38, "right alt": 0x38,
        "space": 0x39, "caps lock": 0x3A,
        "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
        "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "f11": 0x57, "f12": 0x58,
        "up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D,
    }
    for i, ch in enumerate("1234567890"):
        name_scan[ch] = 0x02 + i
    for i, ch in enumerate("qwertyuiop"):
        name_scan[ch] = 0x10 + i
    for i, ch in enumerate("asdfghjkl"):
        name_scan[ch] = 0x1E + i
    for i, ch in enumerate("zxcvbnm"):
        name_scan[ch] = 0x2C + i

    scan: dict[int, int] = {}
    for name, code in name_scan.items():
        try:
            key = pygame.key.key_code(name)
        except (ValueError, AttributeError):
            continue  # name not known to this SDL build; skip it
        scan[key] = code
    return scan


def run_sdl_ui(
    *,
    args,
    frame_sync,
    keyboard,
    stop,
    status: dict,
    counters: dict,
    queue_snapshot_save: Callable[[], None],
    queue_demo_toggle: Callable[[], None] | None = None,
    queue_dos_key: Callable[[int, str], None] | None = None,
    ega_render_start: Callable[[int], int],
    live_memory: Callable[[], bytes],
    live_display_start: Callable[[], int],
    live_video_mode: Callable[[], int] | None = None,
    live_video_page: Callable[[], int] | None = None,
    speaker_events=None,
    adlib_events=None,
) -> None:
    """Run the pygame display loop until the window closes or ``stop`` is set.

    The emulator thread is already running and publishing one frame at a time
    through ``frame_sync``; this loop consumes those frames, decodes them with
    NumPy, scales them with SDL, and feeds keyboard input back to ``keyboard``.
    """
    import pygame

    video = args.video
    palette = args.palette
    scale = max(1, int(args.scale))

    if video == "ega":
        decode = lambda snap, ds: render_ega_rgb(snap, ds)
    elif video == "tandy":
        decode = lambda snap, ds: render_tandy_rgb(snap)
    elif video == "vga":
        decode = lambda snap, ds: render_vga_rgb(snap)
    else:
        decode = lambda snap, ds: render_cga_rgb(snap, palette)

    mixer_buffer = 1024 if getattr(args, "sound", "pc") == "adlib" else 512
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=mixer_buffer)
    pygame.init()
    speaker = PcSpeakerAudio(pygame)
    pygame.display.set_caption(f"PRE2 (emulated {video.upper()})")
    screen = pygame.display.set_mode((WIDTH * scale, HEIGHT * scale), pygame.RESIZABLE)
    scan = _build_pygame_scan()
    adlib_enabled = getattr(args, "sound", "pc") == "adlib" and getattr(args, "adlib_audio", "auto") != "off"
    adlib = (
        NukedAdlibAudio(pygame, status, enabled=adlib_enabled, chunk_ms=getattr(args, "adlib_chunk_ms", 46.0))
        if adlib_events is not None else None
    )
    last_present: dict[str, tuple[bytes, int, int | None, int] | None] = {"value": None}

    def drain_speaker_events() -> None:
        if speaker_events is None:
            return
        while True:
            try:
                enabled, freq = speaker_events.get_nowait()
            except Empty:
                break
            speaker.set(enabled, freq)

    def drain_adlib_events() -> None:
        if adlib_events is None or adlib is None:
            return
        while True:
            try:
                reg, value = adlib_events.get_nowait()
            except Empty:
                break
            adlib.write(reg, value)
        adlib.pump()

    def present(snapshot: bytes, display_start: int, video_mode: int | None = None, video_page: int = 0) -> None:
        if video_mode is not None and (video_mode & 0xFF) in _TEXT_MODES:
            surf = render_text_surface(pygame, snapshot, video_mode & 0xFF, video_page & 0xFF)
        else:
            if video_mode is not None and (video_mode & 0x7F) in (0x13, 0x19):
                rgb = render_vga_rgb(snapshot)
            else:
                rgb = decode(snapshot, display_start)                        # (200,320,3)
            surf = pygame.image.frombuffer(rgb.tobytes(), (WIDTH, HEIGHT), "RGB")
        win_w, win_h = screen.get_size()
        native_w, native_h = surf.get_size()
        fit = max(1, min(win_w // native_w, win_h // native_h))
        target = (native_w * fit, native_h * fit)
        if fit != 1:
            surf = pygame.transform.scale(surf, target)
        x = (win_w - target[0]) // 2
        y = (win_h - target[1]) // 2
        screen.fill((0, 0, 0))
        screen.blit(surf, (x, y))
        pygame.display.flip()
        last_present["value"] = (snapshot, display_start, video_mode, video_page)

    def redraw_last_present() -> None:
        last = last_present["value"]
        if last is not None:
            present(*last)

    def caption() -> None:
        base = f"PRE2 (emulated {video.upper()})"
        c = counters
        tail = (f"visible={c['visible']['n']} boundaries={c['boundary']['n']} "
                f"blits={c['blits']['n']} timers={c['timers']['n']} retraces={c['retraces']['n']}")
        if "direct_video" in c:
            tail += f" direct={c['direct_video']['n']}"
        if c["boundary"]["n"] == 0 and not status["text"]:
            pygame.display.set_caption("PRE2 - running DOS VM...")
        elif status["text"]:
            pygame.display.set_caption(f"{base}  |  {status['text']}  |  {tail}")
        else:
            pygame.display.set_caption(f"{base}  |  {tail}")

    last_caption = 0.0
    try:
        running = True
        while running and not stop.is_set():
            drain_speaker_events()
            drain_adlib_events()
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.VIDEORESIZE:
                    screen = pygame.display.set_mode((max(WIDTH, ev.w), max(HEIGHT, ev.h)), pygame.RESIZABLE)
                    redraw_last_present()
                elif ev.type in (getattr(pygame, "VIDEOEXPOSE", -1), getattr(pygame, "WINDOWEXPOSED", -2)):
                    redraw_last_present()
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_F12:
                        queue_snapshot_save()
                    elif ev.key == pygame.K_F11 and queue_demo_toggle is not None:
                        queue_demo_toggle()
                    else:
                        sc = scan.get(ev.key)
                        if sc is not None:
                            keyboard.post_down(sc)
                            if queue_dos_key is not None:
                                queue_dos_key(sc, getattr(ev, "unicode", ""))
                elif ev.type == pygame.KEYUP:
                    sc = scan.get(ev.key)
                    if sc is not None:
                        keyboard.post_up(sc)

            pending = frame_sync.take_pending()
            if pending is not None:
                if len(pending) == 3:
                    frame_id, snapshot, display_start = pending
                    video_mode, video_page = None, 0
                else:
                    frame_id, snapshot, display_start, video_mode, video_page = pending
                present(snapshot, display_start, video_mode, video_page)
                frame_sync.mark_displayed(frame_id)
            else:
                # No frame ready: yield the GIL so the emulator thread runs.
                pygame.time.wait(1)

            now = pygame.time.get_ticks() / 1000.0
            if now - last_caption > 0.25:
                caption()
                last_caption = now
    finally:
        speaker.close()
        if adlib is not None:
            adlib.close()
        stop.set()
        frame_sync.close()
        pygame.quit()
