"""Checkpoint for the .SQZ asset decompressor (1030:1068).

Recovered logic: ``pre2.codecs.sqz``. Merge target: the asset loader.

Original-binary contract (verified vs PRE2.EXE): entry opens the file named at
1A13:DX, takes the output segment from the bump allocator [1A13:2871], decodes,
returns ax = out_seg to the caller at 1030:00EF, advancing the allocator by the
per-format paragraph count. The caller push/pops ds/es around the call and only
reads ax, so ds/es and decode scratch are caller-dead (not part of the contract).
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.codecs.sqz import sqz_bump_advance, unpack_sqz

from .common import _BUMP_PTR, _DATA_SEG, Pre2HybridGap, _read_cstring, report

_SQZ_SEG = 0x1030
_VAR_OUT_SEG = 0x11F3
_DECOMP_ENTRY = (0x1030, 0x1068)
# The decompressor's own RET sites (ax=out_seg, [2871] bumped, output written) —
# a robust verify boundary that pairs each decode with its completion regardless
# of which caller invoked it. LZSS exits at 15EF, LZW at 1328, "other" at 11F0.
_DECOMP_EXITS = ((0x1030, 0x15EF), (0x1030, 0x1328), (0x1030, 0x11F0))


def _native_decode(cpu):
    """Return ``(name, decompressed_bytes, bump_advance)``; ``out`` is None if
    not natively recovered.

    Uses the DOS machine's own case-insensitive path resolution so the hook sees
    exactly the file the ASM would. ``bump_advance`` is the paragraph count the
    original advances its output allocator by — derived per-format exactly as the
    ASM does (see :func:`pre2.codecs.sqz.sqz_bump_advance`), so the next asset
    lands on the same segment the original would use.
    """
    dos = getattr(cpu, "pre2_dos", None)
    if dos is None:
        return None, None, 0
    name = _read_cstring(cpu.mem, _DATA_SEG, cpu.s.dx)
    try:
        raw = dos.resolve_game_path(name).read_bytes()
        return name, unpack_sqz(raw), sqz_bump_advance(raw)
    except (FileNotFoundError, NotImplementedError, IndexError, ValueError, OSError):
        return name, None, 0


def _expected_bump(out_seg: int, advance: int) -> int:
    return (out_seg + advance) & 0xFFFF


def _commit_native(cpu, out_seg: int, out: bytes, advance: int) -> None:
    """Write the contract the original would have produced, then near-ret."""
    mem = cpu.mem
    base = (out_seg << 4) & 0xFFFFF
    mem.data[base : base + len(out)] = out
    mem.ww(_DATA_SEG, _BUMP_PTR, _expected_bump(out_seg, advance))
    mem.ww(_SQZ_SEG, _VAR_OUT_SEG, out_seg)
    cpu.s.ax = out_seg & 0xFFFF
    cpu.s.ip = cpu.pop()  # near ret to caller (1030:00EF)


@registry.replace(_SQZ_SEG, 0x1068, "sqz_decompress")
def sqz_decompress(cpu) -> None:
    """Native replacement for the original .SQZ decompressor at 1030:1068.

    Hybrid (default): decode natively and return. A not-yet-recovered format or
    unreadable asset raises :class:`Pre2HybridGap` — the hybrid runtime never
    silently falls back to the ASM. Verify mode is different: the original ASM is
    the oracle, so the hook arms recovered decodes for the return-boundary diff
    and lets the ASM execute everything (unrecovered formats are simply not
    diffed yet, not hidden).
    """
    name, out, advance = _native_decode(cpu)
    verify = getattr(cpu, "pre2_verify_mode", False)

    if out is None:
        if verify:
            interpret_current_instruction_without_hook(cpu)  # ASM oracle decodes it
            return
        raise Pre2HybridGap(
            f"hybrid SQZ decompress of {name!r} at 1030:1068 is not recovered "
            "(unrecognised format or unreadable asset). Recover this path — the "
            "hybrid runtime must not silently fall back to ASM."
        )

    out_seg = cpu.mem.rw(_DATA_SEG, _BUMP_PTR)
    if verify:
        cpu.pre2_verify_pending.append((name, out_seg, out, advance))
        interpret_current_instruction_without_hook(cpu)
        return
    _commit_native(cpu, out_seg, out, advance)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hooks at the decompressor's RET sites."""

    def _verify_at_exit(c) -> None:
        # Reached the original decompressor's RET (verify mode let the ASM run).
        # Diff the just-completed decode's contract, then perform the RET.
        if c.pre2_verify_pending:
            name, out_seg, native, advance = c.pre2_verify_pending.pop()
            mem = c.mem
            base = (out_seg << 4) & 0xFFFFF
            asm_out = bytes(mem.data[base : base + len(native)])
            if asm_out != native:
                reason = "output bytes"
            elif (c.s.ax & 0xFFFF) != (out_seg & 0xFFFF):
                reason = f"return ax {c.s.ax:04X}!={out_seg:04X}"
            elif mem.rw(_DATA_SEG, _BUMP_PTR) != _expected_bump(out_seg, advance):
                act = (mem.rw(_DATA_SEG, _BUMP_PTR) - out_seg) & 0xFFFF
                reason = f"bump advance act={act} exp={advance} (out={out_seg:04X} dec={len(native)})"
            else:
                reason = None
            report(stats, on_result, raise_on_divergence, name, reason)
        interpret_current_instruction_without_hook(c)  # original near-ret

    for exit_addr in _DECOMP_EXITS:
        cpu.replacement_hooks[exit_addr] = _verify_at_exit
        cpu.hook_names[exit_addr] = "sqz_verify_exit"
