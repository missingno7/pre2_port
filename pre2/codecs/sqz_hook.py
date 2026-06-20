"""In-VM verification hook for the recovered SQZ codec.

This is the thin "narrow connection" the charter calls for: a non-replacing
**checkpoint** at the decompressor boundary. The original ASM still runs and
remains the oracle; on every ``b4 4c`` decompression we re-derive the output with
the recovered native :func:`pre2.codecs.sqz.unpack_sqz` and assert it equals the
bytes the ASM wrote into memory. No gameplay logic lives here — it only reads
original state, calls clean recovered logic, and compares.

The hook taps the decompressor's file lifecycle (a stable, distinctive boundary):
- the main compressed read (INT 21h AH=3F, ``cx>1000``) — record the output
  segment from ``cs:[11F3]`` and the asset path;
- the close (INT 21h AH=3E) — at this point the ASM has finished writing, so
  ``es:di`` is the output end; compare ``mem[out_start:out_end]`` to native.

Install is opt-in (it adds a re-decode per asset) and temporary — once the codec
is promoted to a replacement hook this checkpoint is what keeps it honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .sqz import unpack_sqz

# The decompressor's code segment and the in-code word holding the output
# segment (cs:[11F3]); see the disassembly at 1030:149B `mov es,[11F3]`.
_SQZ_CODE_SEG = 0x1030
_VAR_OUT_SEG = 0x11F3


@dataclass
class SqzCheckpointStats:
    verified: int = 0
    skipped_unknown: int = 0
    diverged: list[tuple[str, str]] = field(default_factory=list)


def install_sqz_decode_checkpoint(rt, *, on_result=None, raise_on_divergence=False):
    """Verify every ``b4 4c`` SQZ decompression against the native codec, live.

    Returns a :class:`SqzCheckpointStats` that accumulates as the VM runs. With
    ``raise_on_divergence`` set, the first mismatch raises ``AssertionError`` at
    the exact asset — turning any future drift between ASM and native into a hard
    failure instead of silent corruption.
    """
    cpu = rt.cpu
    mem = rt.program.memory
    dos = rt.dos
    orig_int21 = dos.int21
    base = (_SQZ_CODE_SEG << 4) & 0xFFFFF
    pending: dict[int, tuple[Path, int]] = {}
    stats = SqzCheckpointStats()

    def _checkpoint(path: Path, out_seg: int) -> None:
        raw = path.read_bytes()
        try:
            native = unpack_sqz(raw)
        except NotImplementedError:
            stats.skipped_unknown += 1  # format not recovered (e.g. sample/theend)
            return
        out_start = (out_seg << 4) & 0xFFFFF
        out_end = ((cpu.s.es << 4) + cpu.s.di) & 0xFFFFF
        asm_out = bytes(mem.data[out_start:out_end])
        name = path.name
        if native == asm_out:
            stats.verified += 1
            if on_result is not None:
                on_result(name, True, None)
            return
        n = min(len(native), len(asm_out))
        first = next((i for i in range(n) if native[i] != asm_out[i]), n)
        detail = f"len native={len(native)} asm={len(asm_out)} first_diff={first}"
        stats.diverged.append((name, detail))
        if on_result is not None:
            on_result(name, False, detail)
        if raise_on_divergence:
            raise AssertionError(f"SQZ checkpoint divergence on {name}: {detail}")

    def int21(c):
        ah = (c.s.ax >> 8) & 0xFF
        if ah == 0x3F and c.s.cx > 1000:
            fh = dos.files.get(c.s.bx)
            if fh is not None and str(fh.path).lower().endswith(".sqz") and c.s.bx not in pending:
                out_seg = mem.data[base + _VAR_OUT_SEG] | (mem.data[base + _VAR_OUT_SEG + 1] << 8)
                pending[c.s.bx] = (Path(fh.path), out_seg)
            orig_int21(c)
            return
        if ah == 0x3E and c.s.bx in pending:
            path, out_seg = pending.pop(c.s.bx)
            _checkpoint(path, out_seg)
            orig_int21(c)
            return
        orig_int21(c)

    dos.int21 = int21
    return stats
