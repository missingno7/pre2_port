"""SDL/pygame display + audio backend for the interactive DOS_RE viewer (``play.py``).

PRE2 uses BIOS text, VGA DAC palettes, and EGA/VGA-compatible planar graphics
(mode 0Dh) for the game screens; early bring-up snapshots may also use linear
VGA mode 13h.  This module provides those decoders plus the Nuked-OPL3 audio backend.  Frames
are decoded with vectorised NumPy at native 320x200 (independent of ``--scale``)
and uploaded straight to an SDL surface, which keeps the present round-trip cheap
enough to stay interactive.

The graphics decoders are pixel-identical to the reference PPM decoders in
``render_frame.py`` (asserted by ``tests/test_render_rgb.py``); that renderer
remains the headless PNG-dump tool and decode oracle, while this module is what
the live viewer uses.

``play.py`` imports this module only when it actually launches the viewer, so the
core runtime, the PNG tool and the tests do not require ``pygame``.
"""
from __future__ import annotations

import numpy as np

from render_frame import DEFAULT_VGA_PALETTE
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

WIDTH, HEIGHT = 320, 200
_PLANAR_ROW_BYTES = 40   # 320 px / 8 px-per-byte, EGA/VGA 16-colour planar (mode 0Dh)

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

    BIOS text-mode screens are character-cell devices, not graphics surfaces.
    Rendering them through pygame's proportional outline font makes them look
    like scaled UI text.  A small ROM-like 5x7 bitmap expanded into an 8x16 cell
    keeps text deterministic, monospace, and nearest-neighbour friendly.  CP437
    box/extended glyphs fall back to '?' until we need them.
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


class SoundBlasterAudio:
    """Play the PCM the emulated Sound Blaster streams over DMA.

    The VM runs the original SB driver, which DMA's 8-bit unsigned PCM (the game's
    software MOD+SFX mix) at the sample rate it programmed.  This drains that
    captured stream, resamples it to the mixer rate, and plays it on its own mixer
    channel so it mixes with the OPL (AdLib) output.
    """

    def __init__(self, pygame, sound_blaster, status: dict | None = None, *, chunk_ms: float = 46.0) -> None:
        self._pygame = pygame
        self._sb = sound_blaster
        self._status = status
        self._available = False
        self._channel = None
        self._rate = 44100
        self._channels = 1
        self._buf = np.zeros(0, dtype=np.int16)
        self._dc = 0.0
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)
        init = pygame.mixer.get_init()
        if init is None:
            return
        self._rate = int(init[0])
        self._channels = int(init[2])
        self._chunk = max(256, int(round(self._rate * max(10.0, float(chunk_ms)) / 1000.0)))
        # The game produces PCM in per-frame bursts (~22/s); play through a jitter
        # buffer so playback never drains between bursts.  Don't start (or restart
        # after an underrun) until this much audio is queued ahead.
        self._lead = int(self._rate * 0.12)   # ~120 ms
        self._started = False
        if pygame.mixer.get_num_channels() < 3:
            pygame.mixer.set_num_channels(4)
        self._channel = pygame.mixer.Channel(2)
        self._available = True

    def pump(self) -> None:
        if not self._available or self._channel is None:
            return
        self._drain()
        if not self._started:
            # Build the lead before (re)starting; avoids click-restart loops.
            if len(self._buf) >= self._lead:
                self._channel.play(self._next_chunk())
                if len(self._buf) >= self._chunk:
                    self._channel.queue(self._next_chunk())
                self._started = True
            return
        if not self._channel.get_busy():
            self._started = False        # underran -> rebuild the lead, don't click-spam
            return
        # Keep one chunk queued ahead, but only ever emit *full* chunks (no silence pad).
        if self._channel.get_queue() is None and len(self._buf) >= self._chunk:
            self._channel.queue(self._next_chunk())

    def _drain(self) -> None:
        sb = self._sb
        if not sb.pcm_out:
            return
        raw = bytes(sb.pcm_out)
        sb.pcm_out.clear()
        src_rate = sb.sample_rate or 8000
        # SB 8-bit DMA is *unsigned* (silence = 0x80); reading it as signed would
        # flip every mid-scale crossing into a full-scale jump (broadband noise).
        sig = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
        # Remove residual DC (a one-pole high-pass with state carried across drains)
        # so any offset in the stream doesn't sit the waveform off-centre.
        sig = self._dc_block(sig)
        n_out = max(1, len(sig) * self._rate // src_rate)  # nearest-neighbour resample
        idx = np.minimum(np.arange(n_out) * src_rate // self._rate, len(sig) - 1)
        self._buf = np.concatenate([self._buf, sig[idx]])
        cap = self._rate                                   # cap buffered latency at ~1s
        if len(self._buf) > cap:
            self._buf = self._buf[-cap:]

    def _dc_block(self, x):
        # Slowly track the DC level across drains and subtract it (dependency-free
        # high-pass).  The estimate moves gradually so there is no per-drain jump.
        if len(x):
            self._dc += 0.06 * (float(x.mean()) - self._dc)
        return np.clip(x - self._dc, -32768, 32767).astype(np.int16)

    def _next_chunk(self):
        # Emit up to one chunk of whatever is buffered (callers gate on having a
        # full chunk for the queue path; the initial play path may emit the lead).
        n = min(self._chunk, len(self._buf)) or 1
        chunk, self._buf = self._buf[:n], self._buf[n:]
        if self._channels > 1:
            chunk = np.repeat(chunk[:, None], self._channels, axis=1)
        return self._pygame.mixer.Sound(buffer=chunk.astype(np.int16).tobytes())

    def close(self) -> None:
        if self._channel is not None:
            self._channel.stop()


def render_vga_rgb(mem: bytes, palette: list[tuple[int, int, int]] | None = None) -> np.ndarray:
    """Decode VGA mode 13h A000:0000 linear 320x200x8bpp to RGB."""
    arr = np.frombuffer(mem, dtype=np.uint8)
    pal = np.array(palette if palette is not None else DEFAULT_VGA_PALETTE, dtype=np.uint8)
    idx = arr[0xA0000:0xA0000 + WIDTH * HEIGHT].reshape(HEIGHT, WIDTH)
    return pal[idx]


def render_planar_rgb(mem: bytes, display_start: int = 0,
                      palette: list[tuple[int, int, int]] | None = None) -> np.ndarray:
    """Decode a 320x200 16-colour planar screen (mode 0Dh) to RGB.

    The VM stores the four bit-planes in its shadow aperture at ``EGA_APERTURE``.
    Each byte is eight horizontal pixels (MSB first); the 4-bit colour index is
    one bit from each plane and is looked up through the live DAC ``palette`` (the
    attribute controller is identity for PRE2's screens).  This is what the viewer
    uses so the mode-0Dh intro/menu screens are visible while the VM still takes
    that 16-colour path instead of true VGA mode 13h.
    """
    arr = np.frombuffer(mem, dtype=np.uint8)
    pal = np.array(palette if palette is not None else DEFAULT_VGA_PALETTE, dtype=np.uint8)
    start = display_start & 0xFFFF
    rowbase = (start + np.arange(HEIGHT) * _PLANAR_ROW_BYTES) & 0xFFFF
    off = (rowbase[:, None] + np.arange(_PLANAR_ROW_BYTES)[None, :]) & 0xFFFF   # (200,40)
    color = np.zeros((HEIGHT, _PLANAR_ROW_BYTES, 8), dtype=np.uint8)
    for plane in range(4):
        plane_bytes = arr[EGA_APERTURE + plane * EGA_PLANE_STRIDE + off]        # (200,40)
        bits = np.unpackbits(plane_bytes[..., None], axis=2)                    # (200,40,8) MSB-first
        color |= bits << plane
    return pal[color.reshape(HEIGHT, WIDTH)]
