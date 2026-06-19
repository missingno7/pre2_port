"""Fast, target-neutral checks for the reusable DOS RE layer.

These tests intentionally avoid game-specific assets, pygame, and long hook suites.
They are the first command to run in constrained automation:

    python scripts/run_tests.py --scope dos-re --no-lint
"""
from __future__ import annotations

from pathlib import Path
import struct

from dos_re.cpu import CPU8086, CPUState
from dos_re.dos import DOSMachine
from dos_re.memory import LoadedProgram, Memory, create_psp, load_mz_program
from dos_re.mz import MZExecutable, MZHeader
from dos_re.runtime import Runtime
from dos_re.verification import (
    GenericHookStop,
    HookVerifierConfig,
    HookVerifyDivergence,
    HookVerifyLimitReached,
    install_hook_verifier,
)


def _run_bytes(code: bytes, steps: int = 10) -> CPU8086:
    mem = Memory()
    mem.load(0x1000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(steps)
    return cpu


def test_dos_re_cpu_smoke_mov_add_hlt() -> None:
    cpu = _run_bytes(bytes.fromhex("b8 34 12 05 01 00 f4"), 3)
    assert cpu.s.ax == 0x1235
    assert cpu.halted


def test_dos_re_rep_movsb_backward_smoke() -> None:
    mem = Memory()
    mem.load(0x1000, 0, bytes([1, 2, 3, 4]))
    code = bytes.fromhex("fd b9 04 00 be 03 00 bf 13 00 f3 a4 f4")
    mem.load(0x2000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x2000, ds=0x1000, es=0x1000, ss=0x2000, sp=0xFFFE))
    cpu.run(6)
    assert mem.block(0x1000, 0x10, 4) == bytes([1, 2, 3, 4])


def test_dos_re_minimal_mz_loader_smoke(tmp_path: Path) -> None:
    # Tiny MZ program: mov ax,1234h ; hlt
    code = bytes.fromhex("b8 34 12 f4")
    header_paragraphs = 2
    image = bytearray(header_paragraphs * 16)
    image[:28] = struct.pack(
        "<14H",
        0x5A4D,  # e_magic
        len(image) + len(code),  # e_cblp, one partial page
        1,  # e_cp
        0,  # e_crlc
        header_paragraphs,
        0,
        0xFFFF,
        0,  # ss
        0xFFFE,  # sp
        0,
        0,  # ip
        0,  # cs
        0x1C,
        0,
    )
    image.extend(code)
    exe = tmp_path / "SMOKE.EXE"
    exe.write_bytes(image)

    program = load_mz_program(exe)
    cpu = CPU8086(program.memory, CPUState(
        cs=program.entry_cs,
        ip=program.entry_ip,
        ds=program.psp_segment,
        es=program.psp_segment,
        ss=program.initial_ss,
        sp=program.initial_sp,
    ))
    cpu.run(2)
    assert cpu.s.ax == 0x1234
    assert cpu.halted


def test_dos_re_hook_verifier_smoke_near_ret_equivalence(tmp_path: Path) -> None:
    # Original routine at 1000:0000: add ax,1 ; ret
    # Caller return target is pushed by the test.  The hook must exactly match
    # the interpreted routine at the near-return boundary.
    mem = Memory()
    create_psp(mem, 0x0FF0)
    mem.load(0x1000, 0, bytes.fromhex("40 c3"))
    mem.ww(0x1000, 0xFFFC, 0x0100)
    header = MZHeader(
        last_page_bytes=0,
        pages=1,
        relocations=0,
        header_paragraphs=2,
        min_extra_paragraphs=0,
        max_extra_paragraphs=0xFFFF,
        ss=0,
        sp=0xFFFC,
        checksum=0,
        ip=0,
        cs=0,
        relocation_table_offset=0x1C,
        overlay_number=0,
    )
    exe = MZExecutable(tmp_path / "SMOKE.EXE", header, b"", (), b"")
    program = LoadedProgram(
        exe=exe,
        memory=mem,
        psp_segment=0x0FF0,
        load_segment=0x1000,
        entry_cs=0x1000,
        entry_ip=0,
        initial_ss=0x1000,
        initial_sp=0xFFFC,
        overlay=b"",
    )
    dos = DOSMachine(tmp_path)
    dos.seed_initial_memory_block(program.psp_segment)
    cpu = CPU8086(mem, CPUState(ax=0x0010, cs=0x1000, ip=0, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFC))
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    rt = Runtime(program, cpu, dos)

    def inc_ret_hook(hook_cpu: CPU8086) -> None:
        old_ax = hook_cpu.s.ax & 0xFFFF
        result = old_ax + 1
        hook_cpu.s.ax = result & 0xFFFF
        hook_cpu.set_add_flags(old_ax, 1, result, 16)
        hook_cpu.s.ip = hook_cpu.mem.rw(hook_cpu.s.ss, hook_cpu.s.sp)
        hook_cpu.s.sp = (hook_cpu.s.sp + 2) & 0xFFFF

    key = (0x1000, 0x0000)
    cpu.replacement_hooks[key] = inc_ret_hook
    cpu.hook_names[key] = "smoke_inc_ret"
    install_hook_verifier(
        rt,
        HookVerifierConfig(verify_all=True, max_verified=1, stop_on_diff=True, full_memory=False),
        {key: GenericHookStop("near_ret")},
    )

    try:
        cpu.step()
    except HookVerifyLimitReached:
        pass
    else:
        raise AssertionError("hook verifier did not count the smoke hook")

    assert cpu.s.ax == 0x0011
    assert cpu.s.ip == 0x0100



def _make_verifier_smoke_runtime(tmp_path: Path, *, ax: int = 0x0010) -> tuple[Runtime, CPU8086, tuple[int, int]]:
    mem = Memory()
    create_psp(mem, 0x0FF0)
    # Original routine at 1000:0000: add ax,1 ; ret
    mem.load(0x1000, 0, bytes.fromhex("40 c3"))
    mem.ww(0x1000, 0xFFFC, 0x0100)
    header = MZHeader(
        last_page_bytes=0,
        pages=1,
        relocations=0,
        header_paragraphs=2,
        min_extra_paragraphs=0,
        max_extra_paragraphs=0xFFFF,
        ss=0,
        sp=0xFFFC,
        checksum=0,
        ip=0,
        cs=0,
        relocation_table_offset=0x1C,
        overlay_number=0,
    )
    exe = MZExecutable(tmp_path / "SMOKE.EXE", header, b"", (), b"")
    program = LoadedProgram(
        exe=exe,
        memory=mem,
        psp_segment=0x0FF0,
        load_segment=0x1000,
        entry_cs=0x1000,
        entry_ip=0,
        initial_ss=0x1000,
        initial_sp=0xFFFC,
        overlay=b"",
    )
    dos = DOSMachine(tmp_path)
    dos.seed_initial_memory_block(program.psp_segment)
    cpu = CPU8086(mem, CPUState(ax=ax, cs=0x1000, ip=0, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFC))
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    return Runtime(program, cpu, dos), cpu, (0x1000, 0x0000)


def test_dos_re_strict_hook_verifier_auto_continuation_needs_no_metadata(tmp_path: Path) -> None:
    rt, cpu, key = _make_verifier_smoke_runtime(tmp_path)

    def inc_ret_hook(hook_cpu: CPU8086) -> None:
        old_ax = hook_cpu.s.ax & 0xFFFF
        result = old_ax + 1
        hook_cpu.s.ax = result & 0xFFFF
        hook_cpu.set_add_flags(old_ax, 1, result, 16)
        hook_cpu.s.ip = hook_cpu.mem.rw(hook_cpu.s.ss, hook_cpu.s.sp)
        hook_cpu.s.sp = (hook_cpu.s.sp + 2) & 0xFFFF

    cpu.replacement_hooks[key] = inc_ret_hook
    cpu.hook_names[key] = "smoke_inc_ret"
    install_hook_verifier(
        rt,
        HookVerifierConfig.strict(verify_all=True, max_verified=1),
        stops={},
    )

    try:
        cpu.step()
    except HookVerifyLimitReached:
        pass
    else:
        raise AssertionError("strict hook verifier did not count the smoke hook")

    assert cpu.s.ax == 0x0011
    assert cpu.s.ip == 0x0100


def test_dos_re_strict_hook_verifier_auto_continuation_catches_bad_hook_without_metadata(tmp_path: Path) -> None:
    rt, cpu, key = _make_verifier_smoke_runtime(tmp_path)

    def bad_inc_ret_hook(hook_cpu: CPU8086) -> None:
        hook_cpu.s.ax = (hook_cpu.s.ax + 2) & 0xFFFF
        hook_cpu.s.ip = hook_cpu.mem.rw(hook_cpu.s.ss, hook_cpu.s.sp)
        hook_cpu.s.sp = (hook_cpu.s.sp + 2) & 0xFFFF

    cpu.replacement_hooks[key] = bad_inc_ret_hook
    cpu.hook_names[key] = "bad_inc_ret"
    install_hook_verifier(
        rt,
        HookVerifierConfig.strict(verify_all=True),
        stops={},
    )

    divergence: HookVerifyDivergence | None = None
    try:
        cpu.step()
    except HookVerifyDivergence as exc:
        divergence = exc
        report = str(exc)
    else:
        raise AssertionError("strict verifier accepted an intentionally wrong hook")

    assert divergence is not None
    assert "1000:0000 bad_inc_ret" in report
    assert "AX: asm=0011 hook=0012" in report
    assert divergence.repro_runtime is not None
    assert divergence.repro_runtime.cpu.addr() == key
    assert divergence.repro_runtime.cpu.s.ax == 0x0010
    assert cpu.s.ax == 0x0012


def test_dos_re_strict_auto_continuation_reference_ignores_other_replacement_hooks(tmp_path: Path) -> None:
    """Strict mode must run the reference side as original ASM, not hybrid Python.

    The verified routine at 1000:0000 calls 1000:0010.  We deliberately install a
    bad replacement at 0010.  The live parent hook models the original child
    effect directly, so verification should pass only if the ASM oracle ignores
    the unrelated bad child hook and interprets the original bytes.
    """
    mem = Memory()
    create_psp(mem, 0x0FF0)
    # 0000: call 0010 ; ret
    # 0010: add ax,1 ; ret
    mem.load(0x1000, 0x0000, bytes.fromhex("e8 0d 00 c3"))
    mem.load(0x1000, 0x0010, bytes.fromhex("40 c3"))
    mem.ww(0x1000, 0xFFFC, 0x0100)
    header = MZHeader(
        last_page_bytes=0,
        pages=1,
        relocations=0,
        header_paragraphs=2,
        min_extra_paragraphs=0,
        max_extra_paragraphs=0xFFFF,
        ss=0,
        sp=0xFFFC,
        checksum=0,
        ip=0,
        cs=0,
        relocation_table_offset=0x1C,
        overlay_number=0,
    )
    exe = MZExecutable(tmp_path / "SMOKE.EXE", header, b"", (), b"")
    program = LoadedProgram(
        exe=exe,
        memory=mem,
        psp_segment=0x0FF0,
        load_segment=0x1000,
        entry_cs=0x1000,
        entry_ip=0,
        initial_ss=0x1000,
        initial_sp=0xFFFC,
        overlay=b"",
    )
    dos = DOSMachine(tmp_path)
    dos.seed_initial_memory_block(program.psp_segment)
    cpu = CPU8086(mem, CPUState(ax=0x0010, cs=0x1000, ip=0, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFC))
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    rt = Runtime(program, cpu, dos)

    parent_key = (0x1000, 0x0000)
    child_key = (0x1000, 0x0010)

    def parent_hook(hook_cpu: CPU8086) -> None:
        old_ax = hook_cpu.s.ax & 0xFFFF
        result = old_ax + 1
        hook_cpu.s.ax = result & 0xFFFF
        hook_cpu.set_add_flags(old_ax, 1, result, 16)
        # The original parent executes CALL 0010 before the parent RET, leaving
        # the child return address as freed stack scratch below SP.
        hook_cpu.mem.ww(hook_cpu.s.ss, (hook_cpu.s.sp - 2) & 0xFFFF, 0x0003)
        hook_cpu.s.ip = hook_cpu.mem.rw(hook_cpu.s.ss, hook_cpu.s.sp)
        hook_cpu.s.sp = (hook_cpu.s.sp + 2) & 0xFFFF

    def bad_child_hook(hook_cpu: CPU8086) -> None:
        hook_cpu.s.ax = 0xDEAD
        hook_cpu.s.ip = hook_cpu.mem.rw(hook_cpu.s.ss, hook_cpu.s.sp)
        hook_cpu.s.sp = (hook_cpu.s.sp + 2) & 0xFFFF

    cpu.replacement_hooks[parent_key] = parent_hook
    cpu.hook_names[parent_key] = "parent_models_call"
    cpu.replacement_hooks[child_key] = bad_child_hook
    cpu.hook_names[child_key] = "bad_child_must_not_pollute_reference"
    install_hook_verifier(
        rt,
        HookVerifierConfig.strict(hooks={parent_key}, max_verified=1),
        stops={},
    )

    try:
        cpu.step()
    except HookVerifyLimitReached:
        pass
    else:
        raise AssertionError("strict verifier did not verify the parent hook")

    assert cpu.s.ax == 0x0011
    assert cpu.s.ip == 0x0100
