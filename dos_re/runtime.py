from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cpu import CPU8086, CPUState
from .dos import DOSMachine
from .memory import LoadedProgram, load_mz_program
from .hooks import registry


@dataclass
class Runtime:
    program: LoadedProgram
    cpu: CPU8086
    dos: DOSMachine


def create_runtime(
    exe_path: str | Path,
    *,
    game_root: str | Path | None = None,
    command_tail: bytes | str = b"",
) -> Runtime:
    if isinstance(command_tail, str):
        command_tail = command_tail.encode("ascii")
    exe_path = Path(exe_path)
    program = load_mz_program(exe_path, command_tail=command_tail)
    state = CPUState(
        ax=0,
        bx=0,
        cx=0,
        dx=0,
        sp=program.initial_sp,
        bp=0,
        si=0,
        di=0,
        cs=program.entry_cs,
        ip=program.entry_ip,
        ds=program.psp_segment,
        es=program.psp_segment,
        ss=program.initial_ss,
    )
    cpu = CPU8086(program.memory, state)
    root = Path(game_root) if game_root else exe_path.parent
    dos = DOSMachine(root)
    dos.seed_initial_memory_block(program.psp_segment)
    _init_bios_environment(program.memory)
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    registry.install(cpu)
    return Runtime(program, cpu, dos)


# A real BIOS leaves the machine in a known state before a program runs: the
# hardware-IRQ interrupt vectors point at an IRET stub, and the BIOS data area
# holds the video config.  Programs rely on both (e.g. chaining the previous IRQ
# vector, or reading the CRTC base port at 0040:0063).  None of this is
# program-specific — it is the power-on environment any DOS binary expects.
_BIOS_IRET_STUB = 0xFFF53  # F000:FF53, the conventional BIOS dummy IRET


def _init_bios_environment(memory) -> None:
    data = memory.data
    data[_BIOS_IRET_STUB] = 0xCF  # IRET (written directly; F000 is ROM-protected via wb/ww)
    seg, off = 0xF000, 0xFF53
    for vec in (*range(0x08, 0x10), *range(0x70, 0x78)):  # IRQ0-7 (INT 08-0F), IRQ8-15 (INT 70-77)
        base = vec * 4
        if data[base:base + 4] == b"\x00\x00\x00\x00":
            data[base], data[base + 1] = off & 0xFF, (off >> 8) & 0xFF
            data[base + 2], data[base + 3] = seg & 0xFF, (seg >> 8) & 0xFF
    # BIOS data area: CRTC base port (color) — read by retrace-wait code via
    # flat 0463h.  Kept minimal; the game manages the rest of its video state.
    data[0x463], data[0x464] = 0xD4, 0x03   # 0040:0063 = 03D4h


def enable_sound_blaster(rt: Runtime, *, base: int = 0x220, irq: int = 7, dma: int = 1,
                         detection_only: bool = False):
    """Attach an emulated Sound Blaster + PIC so the program detects and uses it.

    Opt-in (an interactive front-end calls this); the deterministic demo/test path
    leaves the hardware absent so its timing is unchanged.  The front-end decides
    *how* to deliver IRQs: at batch boundaries (``pic.acknowledge`` + a forced
    ``deliver_interrupt``) to avoid interrupting the game mid-render, or inline via
    ``rt.cpu.pending_irq`` for tight detection loops.

    ``detection_only`` attaches a *detection stub* (see :class:`SoundBlaster`): the
    program detects a digital device and emits its audio commands, but no PCM is
    streamed and no playback IRQs fire — for front-ends that produce the audio with
    their own (e.g. recovered/native) engine and only need the command stream.
    """
    from .pic import PIC8259
    from .sblaster import SoundBlaster

    pic = PIC8259(imr=0x00)  # nothing masked; only IRQ0/IRQ7 are ever raised here
    sb = SoundBlaster(
        base=base, irq=irq, dma=dma,
        raise_irq=pic.raise_irq,
        read_mem=lambda a: rt.cpu.mem.data[a & 0xFFFFF],
        detection_only=detection_only,
    )
    rt.dos.pic = pic
    rt.dos.sound_blaster = sb
    # Resuming a snapshot taken mid-playback: restore the DSP/DMA programming and
    # re-arm a block IRQ so the driver's refill ISR fires and streaming continues.
    # (The PIC is left fresh — imr=0x00 is the proven cold-boot state and the game
    # re-syncs its mask via port 0x21 at runtime.)
    saved = getattr(rt.dos, "sound_blaster_snapshot", None)
    if saved:
        sb.restore_state(saved)
        sb.rearm_after_restore()
        rt.dos.sound_blaster_snapshot = None
    return sb
