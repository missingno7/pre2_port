"""Native replacement hooks — the hybrid runtime for Prehistorik 2.

Each recovered subsystem is installed as a thin adapter at the original routine's
CS:IP via the shared ``registry``. In normal play these run **instead of** the
original ASM (the hybrid runtime gets faster as coverage grows). Under
verification they run as a parallel oracle check instead.

General mechanism (kept deliberately small to avoid per-hook swell):
- a pure, VM-independent recovered function (e.g. ``pre2.codecs.sqz.unpack_sqz``);
- a thin adapter that reads original VM state, calls the pure function, writes
  the *contract* back (the game-visible outputs), and returns to original flow;
- one verification path that diffs that same contract against the original ASM.

Install with :func:`install_pre2_replacements` (hybrid, default) and optionally
:func:`enable_pre2_hook_verification` (the lockstep oracle, opt-in).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.codecs.sqz import sqz_reserved_size, unpack_sqz

# ---- original-binary contract for the .SQZ decompressor (1030:1068) ----------
# Verified against PRE2.EXE: entry opens the file named at 1A13:DX, takes the
# output segment from the bump allocator [1A13:2871], decodes, and returns
# ax = out_seg to the caller at 1030:00EF, advancing the allocator by
# (size>>4)+1 paragraphs. The caller push/pops ds/es around the call and only
# reads ax, so ds/es and decode scratch are caller-dead (not part of the contract).
_DATA_SEG = 0x1A13
_BUMP_PTR = 0x2871
_SQZ_SEG = 0x1030
_VAR_OUT_SEG = 0x11F3
_DECOMP_ENTRY = (0x1030, 0x1068)
# The decompressor's own RET sites (ax=out_seg, [2871] bumped, output written) —
# a robust verify boundary that pairs each decode with its completion regardless
# of which caller invoked it. LZSS exits at 15EF, LZW at 1328, "other" at 11F0.
_DECOMP_EXITS = ((0x1030, 0x15EF), (0x1030, 0x1328), (0x1030, 0x11F0))


def _read_cstring(mem, seg: int, off: int) -> str:
    base = ((seg << 4) + off) & 0xFFFFF
    end = mem.data.find(0, base, base + 128)
    if end < 0:
        end = base + 128
    return mem.data[base:end].decode("latin1")


class Pre2HybridGap(RuntimeError):
    """The hybrid runtime reached something not yet recovered.

    Raised loudly instead of silently falling back to the original ASM — a silent
    fallback would hide missing recovery work (see the "fail-fast over guessed
    fallback" rule in docs/dos_re/source_port_methodology.md). The remaining
    SQZ "other" format (Huffman+RLE, used by sample/theend) is such a gap today.
    """


def _native_decode(cpu):
    """Return ``(name, decompressed_bytes, reserved_size)``; ``out`` is None if
    not natively recovered.

    Uses the DOS machine's own case-insensitive path resolution so the hook sees
    exactly the file the ASM would. ``reserved_size`` is the original's output
    reservation (header field), which the bump allocator advances by — it can
    exceed ``len(out)`` for LZSS, so it must drive the bump, not the decode length.
    """
    dos = getattr(cpu, "pre2_dos", None)
    if dos is None:
        return None, None, 0
    name = _read_cstring(cpu.mem, _DATA_SEG, cpu.s.dx)
    try:
        raw = dos.resolve_game_path(name).read_bytes()
        return name, unpack_sqz(raw), sqz_reserved_size(raw)
    except (FileNotFoundError, NotImplementedError, IndexError, ValueError, OSError):
        return name, None, 0


def _expected_bump(out_seg: int, reserved: int) -> int:
    return (out_seg + (reserved >> 4) + 1) & 0xFFFF


def _commit_native(cpu, out_seg: int, out: bytes, reserved: int) -> None:
    """Write the contract the original would have produced, then near-ret."""
    mem = cpu.mem
    base = (out_seg << 4) & 0xFFFFF
    mem.data[base : base + len(out)] = out
    mem.ww(_DATA_SEG, _BUMP_PTR, _expected_bump(out_seg, reserved))
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
    name, out, reserved = _native_decode(cpu)
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
        cpu.pre2_verify_pending.append((name, out_seg, out, reserved))
        interpret_current_instruction_without_hook(cpu)
        return
    _commit_native(cpu, out_seg, out, reserved)


# ---- sprite-sheet decode (1030:42F7 local + 1030:436A shared) ----------------
# Two co-dependent routines that demultiplex the decompressed sprite sheet into
# the planar VRAM cache at level load (see docs/pre2/symbol_ledger.md and
# pre2/recovered/sprite_decode.py). They are replaced together: 42F7 writes the
# [0x25CA] index copy that 436A consumes. Verify mode diffs the planar cache slots
# each writes (plus 42F7's data + register contract) against the ASM at the RET.
from pre2.bridge import sprites as _spr
from pre2.recovered.sprite_decode import PIXEL_BASE

_SPR_LOCAL_ENTRY = (0x1030, 0x42F7)
_SPR_LOCAL_EXIT = (0x1030, 0x4369)
_SPR_SHARED_ENTRY = (0x1030, 0x436A)
_SPR_SHARED_EXIT = (0x1030, 0x43B2)
_VAR_MULT_STORE = 0x2CF1   # [0x2CF1] = the paragraph multiplier byte (42F7)
_VAR_BANK_SELECT = 0x2D86  # [0x2D86] index into the multiplier table
_VAR_BANK_TABLE = 0x2D2C   # [bank_select + 0x2D2C] multiplier
_VAR_INDEX_COPY = 0x25CA   # 42F7 copies the 256-entry index table here; 436A reads it
_SPR_SEQ_INDEX = 0x02      # ASM exits the demux with sequencer map-mask selected,
_SPR_MAP_MASK = 0x08       # mask = plane-3 bit (the last plane written)


def _sprite_mult(mem) -> int:
    select = mem.data[(_DATA_SEG << 4) + _VAR_BANK_SELECT]
    return mem.data[(_DATA_SEG << 4) + _VAR_BANK_TABLE + select]


def _restore_map_mask(cpu) -> None:
    """Leave the EGA sequencer state the ASM demux leaves (index 2, map mask 8)."""
    dos = getattr(cpu, "pre2_dos", None)
    if dos is not None:
        dos._seq_index = _SPR_SEQ_INDEX
        dos._seq_regs[_SPR_SEQ_INDEX] = _SPR_MAP_MASK
    cpu.mem.ega_map_mask = _SPR_MAP_MASK


@registry.replace(*_SPR_LOCAL_ENTRY, "sprite_decode_local")
def sprite_decode_local(cpu) -> None:
    """Native replacement for the local sprite-sheet demux at 1030:42F7."""
    mem = cpu.mem
    src = _spr.sprite_sheet_segment(mem)
    mult = _sprite_mult(mem)
    index_copy = _spr.index_table_copy(mem, src)
    slots = _spr.compute_local_slots(mem, src)
    si = (PIXEL_BASE + 0x80 * len(slots)) & 0xFFFF  # [asm 4367: mov si,bp]

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_sprite_pending.append(
            ("local", slots, {"cf1": mult, "bump": src, "idx": index_copy, "si": si, "ds": src})
        )
        interpret_current_instruction_without_hook(cpu)
        return

    mem.data[(_DATA_SEG << 4) + _VAR_MULT_STORE] = mult
    mem.ww(_DATA_SEG, _BUMP_PTR, src)
    base = (_DATA_SEG << 4) + _VAR_INDEX_COPY
    mem.data[base: base + PIXEL_BASE] = index_copy
    _spr.write_slots(mem, slots)
    _restore_map_mask(cpu)
    cpu.s.si = si
    cpu.s.ds = src & 0xFFFF
    cpu.s.es = 0xA000
    cpu.s.ip = cpu.pop()  # near ret to caller (1030:3F18)


@registry.replace(*_SPR_SHARED_ENTRY, "sprite_decode_shared")
def sprite_decode_shared(cpu) -> None:
    """Native replacement for the shared/union sprite-sheet demux at 1030:436A."""
    mem = cpu.mem
    base = _spr.shared_bank_segment(mem)
    slots = _spr.compute_shared_slots(mem, base)

    if getattr(cpu, "pre2_verify_mode", False):
        cpu.pre2_sprite_pending.append(("shared", slots, {}))
        interpret_current_instruction_without_hook(cpu)
        return

    _spr.write_slots(mem, slots)
    _restore_map_mask(cpu)
    cpu.s.ip = cpu.pop()  # near ret to caller (1030:3FAB)


def install_pre2_replacements(rt) -> int:
    """Install the native replacement hooks (the hybrid runtime). Returns count.

    Note ``dos_re.create_runtime`` already auto-installs every ``@registry.replace``
    hook; this additionally wires the asset resolver the hooks need.
    """
    rt.cpu.pre2_dos = rt.dos
    registry.install(rt.cpu)
    return len(registry.replacements)


def uninstall_pre2_replacements(rt) -> None:
    """Remove the native replacement hooks so the runtime executes pure original
    ASM — used for capturing reference output and as the verification oracle."""
    for key in registry.replacements:
        rt.cpu.replacement_hooks.pop(key, None)
        rt.cpu.hook_names.pop(key, None)


# ---- opt-in lockstep verification -------------------------------------------
@dataclass
class HookVerifyStats:
    verified: int = 0
    diverged: list[tuple[str, str]] = field(default_factory=list)


def enable_pre2_hook_verification(rt, *, on_result=None, raise_on_divergence=False):
    """Run replacement hooks as a parallel oracle check instead of replacing.

    Flips the hooks into verify mode: the original ASM executes (the oracle) and
    each native result is diffed against it at the routine's return boundary,
    over the game-visible *contract* only. Returns live-updating
    :class:`HookVerifyStats`. Meant for offline replay of demos/snapshots.
    """
    cpu = rt.cpu
    cpu.pre2_verify_mode = True
    cpu.pre2_verify_pending = []
    cpu.pre2_sprite_pending = []
    stats = HookVerifyStats()

    def _verify_at_exit(c) -> None:
        # Reached the original decompressor's RET (verify mode let the ASM run).
        # Diff the just-completed decode's contract, then perform the RET.
        if c.pre2_verify_pending:
            name, out_seg, native, reserved = c.pre2_verify_pending.pop()
            mem = c.mem
            base = (out_seg << 4) & 0xFFFFF
            asm_out = bytes(mem.data[base : base + len(native)])
            if asm_out != native:
                reason = "output bytes"
            elif (c.s.ax & 0xFFFF) != (out_seg & 0xFFFF):
                reason = f"return ax {c.s.ax:04X}!={out_seg:04X}"
            elif mem.rw(_DATA_SEG, _BUMP_PTR) != _expected_bump(out_seg, reserved):
                reason = "bump pointer"
            else:
                reason = None
            if reason is None:
                stats.verified += 1
                if on_result is not None:
                    on_result(name, True, None)
            else:
                stats.diverged.append((name, reason))
                if on_result is not None:
                    on_result(name, False, reason)
                if raise_on_divergence:
                    raise AssertionError(f"hook verify divergence on {name}: {reason}")
        interpret_current_instruction_without_hook(c)  # original near-ret

    for exit_addr in _DECOMP_EXITS:
        cpu.replacement_hooks[exit_addr] = _verify_at_exit
        cpu.hook_names[exit_addr] = "sqz_verify_exit"

    def _sprite_verify_exit(kind):
        def _exit(c) -> None:
            pend = next((p for p in c.pre2_sprite_pending if p[0] == kind), None)
            if pend is not None:
                c.pre2_sprite_pending.remove(pend)
                _, slots, side = pend
                mem = c.mem
                reason = None
                for slot, planes in slots.items():
                    if _spr.read_slot(mem, slot) != planes:
                        reason = f"{kind} cache slot {slot}"
                        break
                if reason is None and side:
                    base = (_DATA_SEG << 4) + _VAR_INDEX_COPY
                    if mem.data[(_DATA_SEG << 4) + _VAR_MULT_STORE] != side["cf1"]:
                        reason = "[2CF1] multiplier"
                    elif mem.rw(_DATA_SEG, _BUMP_PTR) != (side["bump"] & 0xFFFF):
                        reason = "[2871] bump/source seg"
                    elif bytes(mem.data[base: base + PIXEL_BASE]) != side["idx"]:
                        reason = "[25CA] index-table copy"
                    elif (c.s.si & 0xFFFF) != side["si"]:
                        reason = f"exit si {c.s.si:04X}!={side['si']:04X}"
                    elif (c.s.ds & 0xFFFF) != (side["ds"] & 0xFFFF):
                        reason = f"exit ds {c.s.ds:04X}!={side['ds']:04X}"
                name = f"sprite_decode_{kind}"
                if reason is None:
                    stats.verified += 1
                    if on_result is not None:
                        on_result(name, True, None)
                else:
                    stats.diverged.append((name, reason))
                    if on_result is not None:
                        on_result(name, False, reason)
                    if raise_on_divergence:
                        raise AssertionError(f"hook verify divergence on {name}: {reason}")
            interpret_current_instruction_without_hook(c)  # original near-ret
        return _exit

    cpu.replacement_hooks[_SPR_LOCAL_EXIT] = _sprite_verify_exit("local")
    cpu.hook_names[_SPR_LOCAL_EXIT] = "sprite_verify_local"
    cpu.replacement_hooks[_SPR_SHARED_EXIT] = _sprite_verify_exit("shared")
    cpu.hook_names[_SPR_SHARED_EXIT] = "sprite_verify_shared"
    return stats
