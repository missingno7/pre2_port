"""Checkpoint for the .SQZ asset decompressor (1030:107B).

Recovered logic: ``pre2.codecs.sqz``. Merge target: the asset loader.

Original-binary contract (verified vs PRE2.EXE): entry opens the file named at
1A0F:DX, takes the output segment from [1A0F:2875], decodes the asset into it, and
returns ax = out_seg to the caller. The caller push/pops ds/es around the call and
sets [1A0F:2875]/[1A0F:003D] = ax itself, so ds/es, the load pointer and the decode
scratch are caller-managed (not part of this routine's contract).
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.codecs.sqz import sqz_bump_advance, unpack_sqz

from .common import Pre2HybridGap, _read_cstring, report

# GOG data segment + the asset load pointer the caller sets before this routine and
# reads back after (where the decoded asset lands): [1A0F:2875].
_DATA_SEG = 0x1A0F
_LOAD_PTR = 0x2875
_DECOMP_ENTRY = (0x1030, 0x107B)
# The decompressor's own RET sites (ax=out_seg, output written) — a robust verify
# boundary that pairs each decode with its completion regardless of which caller
# invoked it. LZSS exits at 1602, LZW at 133B.  ("other"/uncompressed TBD.)
_DECOMP_EXITS = ((0x1030, 0x1602), (0x1030, 0x133B))


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


def _commit_native(cpu, out_seg: int, out: bytes) -> None:
    """Write the contract the original would have produced, then near-ret.

    The decoded asset lands at ``out_seg`` (the load pointer the caller set in
    [1A0F:2875]); the routine returns ``ax = out_seg`` and the caller stores it
    back into the load pointer, so this routine does not touch [2875] itself.
    """
    mem = cpu.mem
    base = (out_seg << 4) & 0xFFFFF
    mem.data[base : base + len(out)] = out
    cpu.s.ax = out_seg & 0xFFFF
    cpu.s.ip = cpu.pop()  # near ret to caller


@registry.replace(*_DECOMP_ENTRY, "sqz_decompress")
def sqz_decompress(cpu) -> None:
    """Native replacement for the original .SQZ decompressor at 1030:107B.

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
            f"hybrid SQZ decompress of {name!r} at 1030:107B is not recovered "
            "(unrecognised format or unreadable asset). Recover this path — the "
            "hybrid runtime must not silently fall back to ASM."
        )

    out_seg = cpu.mem.rw(_DATA_SEG, _LOAD_PTR)
    if verify:
        cpu.pre2_verify_pending.append((name, out_seg, out, advance))
        interpret_current_instruction_without_hook(cpu)
        return
    _commit_native(cpu, out_seg, out)


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
                i = next(k for k in range(len(native)) if asm_out[k] != native[k])
                reason = (f"output@{i} of {len(native)}B: asm={asm_out[i]:02X} rec={native[i]:02X} "
                          f"(out_seg={out_seg:04X})")
            elif (c.s.ax & 0xFFFF) != (out_seg & 0xFFFF):
                reason = f"return ax {c.s.ax:04X}!={out_seg:04X}"
            else:
                # Length check: the recovered output must not be SHORTER than what the ASM
                # decoded.  The contract compared only the first len(native) bytes, so a
                # truncated decode (recovered stops early) passed silently -- yet live it
                # leaves the asset's tail stale and corrupts later loads.  The allocator
                # reserves `advance` paragraphs; if the recovered stopped inside that span
                # and the ASM wrote real bytes past the recovered end, it's truncated.
                reserved = (advance * 16) & 0x1FFFFF
                tail = bytes(mem.data[base + len(native) : base + reserved])
                reason = None
                if tail and any(tail):
                    j = next(k for k in range(len(tail)) if tail[k])
                    reason = (f"TRUNCATED: recovered {len(native)}B but ASM wrote past it "
                              f"(@{len(native) + j}={tail[j]:02X}); reserved {reserved}B")
            report(stats, on_result, raise_on_divergence, name, reason)
        interpret_current_instruction_without_hook(c)  # original near-ret

    for exit_addr in _DECOMP_EXITS:
        cpu.replacement_hooks[exit_addr] = _verify_at_exit
        cpu.hook_names[exit_addr] = "sqz_verify_exit"
