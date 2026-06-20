from pathlib import Path

from dos_re.cpu import CPU8086, CPUState
from dos_re.dos import DOSMachine
from dos_re.memory import Memory


def run_bytes(code: bytes, steps: int = 10):
    mem = Memory()
    mem.load(0x1000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(steps)
    return cpu


def test_mov_add_ret():
    cpu = run_bytes(bytes.fromhex("b8 34 12 05 01 00 f4"), 3)
    assert cpu.s.ax == 0x1235


def test_memory_operand_decoded_once():
    cpu = run_bytes(bytes.fromhex("c7 06 00 01 34 12 81 06 00 01 01 00 f4"), 3)
    assert cpu.mem.rw(0x1000, 0x0100) == 0x1235
    assert cpu.s.ip == 0x000D


def test_hook_verify_range_diff_keeps_exact_mismatch_report():
    from dos_re.verification import HookVerifier, MemoryRange

    asm = bytearray(b"\x00" * 64)
    hook = bytearray(asm)
    rng = MemoryRange("probe", 8, 32)

    assert HookVerifier._range_diff(asm, hook, rng) is None

    hook[12] = 0x34
    hook[30] = 0x56
    report = HookVerifier._range_diff(asm, hook, rng)
    assert report is not None
    assert "differing bytes: 2" in report
    assert "first diff: 0000C asm=00 hook=34" in report


def test_hook_verify_defaults_to_full_memory_image():
    from types import SimpleNamespace
    from dos_re.verification import HookVerifier, HookVerifierConfig

    mem = Memory()
    hv = HookVerifier.__new__(HookVerifier)
    hv.config = HookVerifierConfig()
    rt = SimpleNamespace(
        program=SimpleNamespace(memory=mem),
        cpu=SimpleNamespace(s=CPUState(cs=0x1010, ds=0x2000, es=0x2000, ss=0x2000)),
    )

    ranges = hv._memory_ranges(rt)

    assert len(ranges) == 1
    assert ranges[0].name == "full memory"
    assert ranges[0].start == 0
    assert ranges[0].size == len(mem.data)


def test_rep_movsb_backward():
    mem = Memory()
    mem.load(0x1000, 0, bytes([1, 2, 3, 4]))
    code = bytes.fromhex("fd b9 04 00 be 03 00 bf 13 00 f3 a4 f4")
    mem.load(0x2000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x2000, ds=0x1000, es=0x1000, ss=0x2000, sp=0xFFFE))
    cpu.run(6)
    assert mem.block(0x1000, 0x10, 4) == bytes([1, 2, 3, 4])


def test_outsb_and_rep_outsb_advance_si_and_write_ports():
    mem = Memory()
    mem.load(0x1000, 0, bytes([0x12, 0x34, 0x56]))
    code = bytes.fromhex("ba c8 03 6e b9 02 00 f3 6e f4")
    mem.load(0x2000, 0, code)
    log = []
    cpu = CPU8086(mem, CPUState(cs=0x2000, ds=0x1000, es=0x1000, ss=0x2000, sp=0xFFFE))
    cpu.port_writer = lambda _cpu, port, value, bits: log.append((port, value, bits))
    cpu.run(5)
    assert log == [(0x03C8, 0x12, 8), (0x03C8, 0x34, 8), (0x03C8, 0x56, 8)]
    assert cpu.s.si == 3




def test_386_operand_size_prefix_is_ignored_for_pre2_probe_low_word():
    cpu = run_bytes(bytes.fromhex("b8 34 12 66 33 c0 f4"), 3)
    assert cpu.s.ax == 0
    assert cpu.halted


def test_vga_dac_palette_roundtrip_for_pre2_probe():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))

    dos.port_write(cpu, 0x03C8, 5, 8)
    dos.port_write(cpu, 0x03C9, 0x12, 8)
    dos.port_write(cpu, 0x03C9, 0x23, 8)
    dos.port_write(cpu, 0x03C9, 0x34, 8)
    dos.port_write(cpu, 0x03C7, 5, 8)

    assert dos.port_read(cpu, 0x03C9, 8) == 0x12
    assert dos.port_read(cpu, 0x03C9, 8) == 0x23
    assert dos.port_read(cpu, 0x03C9, 8) == 0x34


def test_ega_latch_rotate_or_write_mode_for_pre2_vga_probe():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x0D

    dos.port_write(cpu, 0x03C4, 0x0102, 16)  # sequencer map-mask: plane 0 only
    mem.wb(0xA000, 0x2000, 0x11)
    dos.port_write(cpu, 0x03CE, 0x0004, 16)  # graphics-controller read plane 0
    assert mem.rb(0xA000, 0x2000) == 0x11  # loads all four VGA latches

    dos.port_write(cpu, 0x03CE, 0x1103, 16)  # rotate right 1, logical OR with latch
    mem.wb(0xA000, 0x2000, 0xA0)

    assert mem.rb(0xA000, 0x2000) == 0x51


def test_ega_write_mode_1_copies_latches_to_destination_planes():
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x0D

    dos.port_write(cpu, 0x03C4, 0x0F02, 16)  # sequencer map-mask: all planes
    source = 0x1234
    dest = 0x2345
    for plane, value in enumerate((0x11, 0x22, 0x44, 0x88)):
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + source] = value
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] = 0x00

    dos.port_write(cpu, 0x03CE, 0x0004, 16)  # read plane 0; read loads all latches
    assert mem.rb(0xA000, source) == 0x11
    dos.port_write(cpu, 0x03CE, 0x0105, 16)  # graphics-controller write mode 1
    mem.wb(0xA000, dest, 0xFF)               # CPU byte is ignored in write mode 1

    for plane, value in enumerate((0x11, 0x22, 0x44, 0x88)):
        assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] == value


def test_ega_write_mode_1_respects_map_mask():
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x0D

    source = 0x0100
    dest = 0x0200
    for plane, value in enumerate((0xA1, 0xB2, 0xC3, 0xD4)):
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + source] = value
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] = 0xEE

    dos.port_write(cpu, 0x03CE, 0x0004, 16)
    mem.rb(0xA000, source)
    dos.port_write(cpu, 0x03CE, 0x0105, 16)
    dos.port_write(cpu, 0x03C4, 0x0A02, 16)  # planes 1 and 3 only
    mem.wb(0xA000, dest, 0x00)

    for plane, value in enumerate((0xEE, 0xB2, 0xEE, 0xD4)):
        assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] == value



def test_cmpsw_compares_ds_si_with_es_di_and_advances():
    mem = Memory()
    mem.ww(0x1000, 0x0100, 0x1234)
    mem.ww(0x2000, 0x0200, 0x1234)
    code = bytes.fromhex("be 00 01 bf 00 02 a7 f4")
    mem.load(0x3000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x3000, ds=0x1000, es=0x2000, ss=0x3000, sp=0xFFFE))
    cpu.run(4)
    assert cpu.s.si == 0x0102
    assert cpu.s.di == 0x0202
    assert cpu.get_flag(0x0040)

def test_dos_version_returns_al_major_ah_minor():
    from dos_re.cpu import CF
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x3000
    dos.interrupt(cpu, 0x21)
    assert cpu.s.ax == 0x0005
    assert not cpu.get_flag(CF)




def test_int2f_xms_probe_reports_driver_absent():
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x4300
    dos.interrupt(cpu, 0x2F)
    assert cpu.s.ax == 0x4300

def test_ega_crtc_display_start_tracks_indexed_port_writes():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))

    dos.port_write(cpu, 0x03D4, 0x120C, 16)
    dos.port_write(cpu, 0x03D4, 0x340D, 16)
    assert mem.ega_display_start == 0x1234

    dos.port_write(cpu, 0x03D4, 0x0C, 8)
    dos.port_write(cpu, 0x03D5, 0x20, 8)
    assert mem.ega_display_start == 0x2034


def test_pre2_runtime_bootstraps_past_lzexe_stub():
    from pre2.runtime import create_pre2_runtime

    root = Path(__file__).resolve().parents[1]
    rt = create_pre2_runtime(root / "assets" / "pre2.exe", game_root=root / "assets")
    rt.cpu.trace_enabled = False
    rt.cpu.run(2_000)

    # The original MZ entry is the LZEXE stub at 1CB6:000E.  Reaching 1996/1C34
    # means the packed executable has materialized real PRE2 program code.
    assert rt.cpu.s.cs in {0x1996, 0x1C34}


def test_int67_ems_probe_reports_driver_absent():
    from dos_re.cpu import CPU8086, CPUState
    from dos_re.dos import DOSMachine
    from dos_re.memory import Memory

    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x4000  # EMS get status
    dos.interrupt(cpu, 0x67)
    assert (cpu.s.ax >> 8) == 0x80


def test_80186_push_immediate_words():
    cpu = run_bytes(bytes.fromhex("68 34 12 6a ff 58 5b f4"), 5)
    assert cpu.s.ax == 0xFFFF
    assert cpu.s.bx == 0x1234


def test_80186_shift_immediate_group2():
    cpu = run_bytes(bytes.fromhex("b0 81 c0 e8 01 bb 00 81 c1 eb 04 f4"), 5)
    assert cpu.s.ax & 0xFF == 0x40
    assert cpu.s.bx == 0x0810


def test_shift_count_zero_preserves_flags():
    cpu = run_bytes(bytes.fromhex("f9 b0 81 c0 e8 20 f4"), 4)
    assert cpu.s.ax & 0xFF == 0x81
    assert cpu.get_flag(0x0001)


def test_rotate_does_not_touch_zero_sign_parity_flags():
    cpu = run_bytes(bytes.fromhex("b0 80 0a c0 d0 d0 f4"), 5)
    # OR AL,AL set SF and clears ZF; RCL AL,1 may change CF but must leave SF/ZF/PF alone.
    assert cpu.get_flag(0x0080)
    assert not cpu.get_flag(0x0040)



def test_segment_override_applies_to_string_source():
    mem = Memory()
    mem.wb(0x1000, 0x0100, 0x11)
    mem.wb(0x2000, 0x0100, 0x22)
    # ES: MOVSB copies from ES:SI to ES:DI. The destination segment is still ES;
    # only the string source segment is overridden.
    mem.load(0x3000, 0, bytes.fromhex("be 00 01 bf 00 02 26 a4 f4"))
    cpu = CPU8086(mem, CPUState(cs=0x3000, ds=0x1000, es=0x2000, ss=0x3000, sp=0xFFFE))
    cpu.run(4)
    assert mem.rb(0x2000, 0x0200) == 0x22
    assert cpu.s.si == 0x0101
    assert cpu.s.di == 0x0201
