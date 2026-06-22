"""Generic Sound Blaster (DSP) + 8237 DMA channel + PIC IRQ — emulated PC hardware.

This is target-neutral hardware, like a slice of DOSBox: a program resets and
probes the DSP, programs a sample rate and a DMA buffer, and the card pulls PCM
bytes from memory over DMA and raises an IRQ at each block boundary.  Nothing here
knows about any particular game; the front-end wires it into the port map and
drains the produced PCM.

Port layout (relative to ``base``, the program discovers ``base`` by scanning):
* ``base+0x6``  reset (write 1 then 0 -> DSP returns 0xAA on the read-data port)
* ``base+0xA``  read data (DSP -> CPU)
* ``base+0xC``  write command/data (CPU -> DSP); read = write-buffer status (bit7=busy)
* ``base+0xE``  read-buffer status (bit7 = data available); reading also ACKs the IRQ
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Number of data bytes each DSP command consumes after the command byte.  Commands
# not listed take none.  This is the standard SB/SB-Pro DSP command set.
_DSP_CMD_ARGS = {
    0x10: 1,  # direct 8-bit DAC output
    0x14: 2,  # 8-bit single-cycle DMA output (length-1, lo/hi)
    0x16: 2,  # 2-bit ADPCM DMA output
    0x17: 2,  # 2-bit ADPCM + reference
    0x24: 2,  # 8-bit single-cycle DMA input
    0x40: 1,  # set time constant
    0x41: 2,  # set output sample rate (hi, lo)
    0x48: 2,  # set DMA block size (lo, hi)
    0x80: 2,  # silence DMA (length)
    0xE0: 1,  # DSP identification (returns ~arg)
    0xE4: 1,  # write test register
}
# Commands with no arguments that we still must recognise.
_DSP_CMD_NOARG = {
    0x1C, 0x90, 0x91, 0x98, 0x99,  # auto-init / high-speed DMA output starts
    0xD0, 0xD1, 0xD3, 0xD4, 0xDA,  # pause/speaker-on/off/continue/exit-auto-init
    0xE1,  # DSP version
    0xE8,  # read test register
    0xF2,  # force 8-bit IRQ
    0xF8,  # undocumented, returns 0
}


@dataclass
class DmaChannel:
    """One 8237 DMA channel (8-bit): base address, count, page, mode, mask."""

    page: int = 0
    base_addr: int = 0
    base_count: int = 0
    cur_addr: int = 0
    cur_count: int = 0
    mode: int = 0
    masked: bool = True
    _flipflop_high: bool = False

    def write_addr(self, value: int) -> None:
        if self._flipflop_high:
            self.base_addr = (self.base_addr & 0x00FF) | (value << 8)
        else:
            self.base_addr = (self.base_addr & 0xFF00) | value
        self._flipflop_high = not self._flipflop_high
        self.cur_addr = self.base_addr

    def write_count(self, value: int) -> None:
        if self._flipflop_high:
            self.base_count = (self.base_count & 0x00FF) | (value << 8)
        else:
            self.base_count = (self.base_count & 0xFF00) | value
        self._flipflop_high = not self._flipflop_high
        self.cur_count = self.base_count

    def physical(self) -> int:
        return ((self.page << 16) | self.cur_addr) & 0xFFFFF

    def snapshot_state(self) -> dict:
        return {
            "page": self.page, "base_addr": self.base_addr, "base_count": self.base_count,
            "cur_addr": self.cur_addr, "cur_count": self.cur_count, "mode": self.mode,
            "masked": self.masked, "flipflop_high": self._flipflop_high,
        }

    def restore_state(self, d: dict) -> None:
        self.page = d.get("page", 0)
        self.base_addr = d.get("base_addr", 0)
        self.base_count = d.get("base_count", 0)
        self.cur_addr = d.get("cur_addr", 0)
        self.cur_count = d.get("cur_count", 0)
        self.mode = d.get("mode", 0)
        self.masked = d.get("masked", True)
        self._flipflop_high = d.get("flipflop_high", False)


@dataclass
class SoundBlaster:
    """SB DSP + its 8-bit DMA channel + IRQ line.  ``base`` is fixed hardware
    config the program *discovers*; we keep the canonical 0x220/IRQ7/DMA1."""

    base: int = 0x220
    irq: int = 7
    dma: int = 1
    raise_irq: Callable[[int], None] | None = None  # called with the IRQ number
    read_mem: Callable[[int], int] | None = None     # phys addr -> byte (DMA fetch)
    # Detection-stub mode: a card that answers the DSP probe + the IRQ-wiring test of the
    # detection handshake, but never streams PCM or sustains continuous playback. The
    # front-end uses this when the actual audio is produced elsewhere (a recovered/native
    # audio engine), so the program still *detects* a digital device and emits its audio
    # commands while the DMA/IRQ block production is gone. Generic (no program knowledge):
    # it services the first ``detect_irq_limit`` block IRQs (the handshake probe) then goes
    # silent. ``detect_irq_limit`` defaults to 1 (one probe DMA + IRQ test).
    detection_only: bool = False
    detect_irq_limit: int = 1
    _dma_requests: int = 0

    channels: dict[int, DmaChannel] = field(default_factory=lambda: {c: DmaChannel() for c in range(4)})
    speaker_on: bool = False
    time_constant: int = 0
    sample_rate: int = 0
    block_len: int = 0          # from 0x48; auto-init block size (samples-1)
    _out: list[int] = field(default_factory=list)   # DSP -> CPU read-data queue
    _resetting: bool = False
    _args_needed: int = 0
    _cmd: int = 0
    _args: list[int] = field(default_factory=list)
    irq_line: bool = False
    log: list[tuple] = field(default_factory=list)
    # auto-init playback bookkeeping
    auto_init: bool = False
    dma_active: bool = False
    pcm_out: bytearray = field(default_factory=bytearray)  # captured 8-bit unsigned PCM stream
    # The block-complete IRQ must fire only after the block's *playback time* so the
    # driver refills at the real sample rate (firing instantly would make it refill
    # in a tight loop).  ``clock`` returns seconds (wall-clock in the live viewer);
    # if None, the IRQ fires immediately (detection-only / headless use).
    clock: Callable[[], float] | None = None
    # Anchor the block cadence to a fixed grid (live play, for drift-free audio).
    # Left False for the deterministic demo clock so recordings stay byte-reproducible.
    anchor_cadence: bool = False
    _block_due: float = 0.0
    _block_pending: bool = False
    # If the front-end falls more than this far behind the block grid (a long render
    # or GC stall), drop the accumulated lateness and re-anchor instead of firing a
    # catch-up storm.  Bare assignment => class attribute, not a dataclass field.
    MAX_BLOCK_CATCHUP = 0.5

    # ---- port interface ------------------------------------------------------
    def owns_port(self, port: int) -> bool:
        return (port & ~0x000F) == self.base

    def port_read(self, port: int) -> int:
        off = port - self.base
        if off == 0xA:                      # read data
            v = self._out.pop(0) if self._out else 0x00
            return v & 0xFF
        if off == 0xC:                      # write-buffer status (bit7=0 -> ready)
            return 0x7F
        if off == 0xE:                      # read-buffer status; reading ACKs the IRQ
            self.irq_line = False
            return 0x80 if self._out else 0x00
        if off == 0x6:                      # reset port reads back nonsense
            return 0xFF
        return 0xFF

    # ---- 8237 DMA controller #1 ports (0x00-0x0F) + page registers (0x80-0x8F) -
    _PAGE_PORT_TO_CH = {0x87: 0, 0x83: 1, 0x81: 2, 0x82: 3}

    def dma_controller_write(self, port: int, value: int) -> None:
        value &= 0xFF
        if port <= 0x07:
            ch = self.channels[port >> 1]
            (ch.write_addr if (port & 1) == 0 else ch.write_count)(value)
        elif port == 0x0A:                  # single mask register
            self.channels[value & 3].masked = bool(value & 4)
        elif port == 0x0B:                  # mode register
            self.channels[value & 3].mode = value
        elif port == 0x0C:                  # clear byte-pointer flip-flop
            for c in self.channels.values():
                c._flipflop_high = False

    def page_write(self, port: int, value: int) -> None:
        ch = self._PAGE_PORT_TO_CH.get(port)
        if ch is not None:
            self.channels[ch].page = value & 0xFF

    def port_write(self, port: int, value: int) -> None:
        off = port - self.base
        value &= 0xFF
        if off == 0x6:                      # reset
            if value & 1:
                self._resetting = True
            elif self._resetting:
                self._resetting = False
                self._out = [0xAA]          # reset complete signature
                self.log.append(("reset", 0xAA))
            return
        if off == 0xC:                      # command / data
            self._feed(value)
            return

    # ---- DSP command FSM -----------------------------------------------------
    def _feed(self, value: int) -> None:
        if self._args_needed:
            self._args.append(value)
            self._args_needed -= 1
            if self._args_needed == 0:
                self._exec()
            return
        self._cmd = value
        self._args = []
        self._args_needed = _DSP_CMD_ARGS.get(value, 0)
        if self._args_needed == 0:
            self._exec()

    def _exec(self) -> None:
        c, args = self._cmd, self._args
        self.log.append((f"cmd_{c:02X}", list(args)))
        if c == 0xE1:                        # DSP version -> 2.1 (SB Pro-ish)
            self._out += [0x02, 0x01]
        elif c == 0xE0:                      # identification: return ~arg
            self._out.append((~args[0]) & 0xFF)
        elif c == 0xE8:
            self._out.append(0x00)
        elif c == 0xD1:
            self.speaker_on = True
        elif c == 0xD3:
            self.speaker_on = False
        elif c == 0x40:
            self.time_constant = args[0]
            self.sample_rate = int(1_000_000 / (256 - args[0])) if args[0] != 256 else 0
        elif c == 0x41:
            self.sample_rate = (args[0] << 8) | args[1]
        elif c == 0x48:
            self.block_len = args[0] | (args[1] << 8)
        elif c == 0x14:                      # 8-bit single-cycle DMA output
            self._start_dma(length=(args[0] | (args[1] << 8)) + 1, auto=False)
        elif c in (0x1C, 0x90):              # 8-bit auto-init DMA output
            self._start_dma(length=self.block_len + 1, auto=True)
        elif c == 0xF2:                      # force IRQ
            self._fire_irq()
        # other no-arg commands (pause/continue/etc.) are accepted silently.

    # ---- DMA / IRQ -----------------------------------------------------------
    def _start_dma(self, *, length: int, auto: bool) -> None:
        self.auto_init = auto
        self.dma_active = True
        self._dma_requests += 1
        if self.detection_only:
            # Detection stub: service the handshake's IRQ-wiring test (the first probe
            # DMA), then never fetch PCM or raise another block IRQ. Playback DMA requests
            # are accepted as no-ops, so the program's audio driver stays in digital mode +
            # keeps emitting commands while the real audio comes from elsewhere.
            self.log.append(("dma_stub", {"len": length, "auto": auto, "n": self._dma_requests}))
            if self._dma_requests <= self.detect_irq_limit:
                self._fire_irq()
            return
        ch = self.channels[self.dma]
        # Pull the block out of memory over DMA (8-bit unsigned PCM) and capture it.
        if self.read_mem is not None:
            addr = ch.physical()
            self.pcm_out.extend(self.read_mem((addr + i) & 0xFFFFF) for i in range(length))
        self.log.append(("dma_start", {"len": length, "auto": auto, "rate": self.sample_rate}))
        # The card raises its IRQ when the block finishes playing.  Pace it by the
        # block's playback time when a clock is available; otherwise fire at once.
        if self.clock is None:
            self._fire_irq()
        else:
            rate = self.sample_rate or 8000
            now = self.clock()
            # Anchor the block-complete cadence to a fixed grid (previous block's due +
            # one block period) rather than `now + period`.  `now` includes the time the
            # ISR took to refill+replay the block (IRQ-servicing lag + the recovered
            # mixer), so the relative form folds that delay into *every* interval and the
            # stream drifts a few % below real time -> the live jitter buffer slowly
            # drains and periodically underruns.  Anchoring keeps the long-run rate
            # exactly `rate`.  A fresh start (no prior due) or a fall too far behind
            # (> MAX_BLOCK_CATCHUP, e.g. a render/GC stall) re-anchors to `now` so the
            # IRQ can't spiral into a catch-up storm.
            if self.anchor_cadence:
                ideal = self._block_due + length / rate
                if not self._block_due or (now - ideal) > self.MAX_BLOCK_CATCHUP:
                    ideal = now + length / rate
            else:
                ideal = now + length / rate
            self._block_due = ideal
            self._block_pending = True

    def service(self) -> None:
        """Fire a due block-complete IRQ (call frequently from the front-end)."""
        if self._block_pending and self.clock is not None and self.clock() >= self._block_due:
            self._block_pending = False
            self._fire_irq()

    def resync_clock(self, now: float) -> None:
        """Re-base a pending block-complete IRQ onto a new clock origin.

        The front-end may switch the time source (e.g. wall clock <-> the
        deterministic demo clock); ``_block_due`` was computed against the old
        origin, so without this the next block would either stall forever or fire
        a catch-up burst.  Re-arm it one block-period from ``now`` on the new clock.
        """
        if self._block_pending:
            rate = self.sample_rate or 8000
            length = self.block_len or 168
            self._block_due = now + length / rate

    def _fire_irq(self) -> None:
        self.irq_line = True
        if self.raise_irq is not None:
            self.raise_irq(self.irq)

    # ---- snapshot persistence ------------------------------------------------
    # The DSP/DMA programming state is part of the machine state: a save taken
    # mid-playback must restore it so the resumed game (already past detection,
    # waiting on the next block-complete IRQ) keeps streaming. Wiring callbacks
    # (raise_irq/read_mem/clock) and the output accumulators (pcm_out/log) are
    # re-attached by the front-end and deliberately not persisted.
    def snapshot_state(self) -> dict:
        return {
            "base": self.base, "irq": self.irq, "dma": self.dma,
            "speaker_on": self.speaker_on, "time_constant": self.time_constant,
            "sample_rate": self.sample_rate, "block_len": self.block_len,
            "auto_init": self.auto_init, "dma_active": self.dma_active,
            "irq_line": self.irq_line, "resetting": self._resetting,
            "args_needed": self._args_needed, "cmd": self._cmd, "args": list(self._args),
            "out": list(self._out), "block_pending": self._block_pending,
            "channels": {str(c): ch.snapshot_state() for c, ch in self.channels.items()},
        }

    def restore_state(self, d: dict) -> None:
        self.base = d.get("base", self.base)
        self.irq = d.get("irq", self.irq)
        self.dma = d.get("dma", self.dma)
        self.speaker_on = d.get("speaker_on", False)
        self.time_constant = d.get("time_constant", 0)
        self.sample_rate = d.get("sample_rate", 0)
        self.block_len = d.get("block_len", 0)
        self.auto_init = d.get("auto_init", False)
        self.dma_active = d.get("dma_active", False)
        self.irq_line = d.get("irq_line", False)
        self._resetting = d.get("resetting", False)
        self._args_needed = d.get("args_needed", 0)
        self._cmd = d.get("cmd", 0)
        self._args = list(d.get("args", []))
        self._out = list(d.get("out", []))
        self._block_pending = d.get("block_pending", False)
        for c, chd in d.get("channels", {}).items():
            self.channels[int(c)].restore_state(chd)

    def rearm_after_restore(self) -> None:
        """Re-arm a block-complete IRQ if a restored snapshot was mid-stream.

        The driver drives continuous playback by re-issuing single-cycle DMA on
        each block IRQ; restoring the programmed state but no pending IRQ would
        leave it waiting forever. Raise the IRQ line directly (the PIC holds it
        until the CPU delivers it, in either inline or batch-boundary mode) so the
        driver's refill ISR runs, re-issues DMA, and the stream self-sustains.
        Clock-independent — works in both the live viewer and headless paths.
        """
        if self.dma_active:
            self._block_pending = False
            self._fire_irq()
