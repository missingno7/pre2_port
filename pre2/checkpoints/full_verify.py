"""Foolproof full-effect lockstep verify.

The per-subsystem verifiers (``register_verify`` in each checkpoint) diff a
*hand-picked contract* — a few planes / state words chosen per routine. That is
fast but leaky: anything a recovered hook writes (or fails to write, or leaves in
the wrong register) **outside** the chosen contract slips through, and can surface
as an unrelated failure much later (e.g. the SQZ ``[2875]`` bump that wasn't in the
contract and crashed the game ~160k instructions later; or ``panel_copy`` leaving
``[3050]/[3052]`` stale).

This mode removes the contract entirely. For each recovered hook it runs the
**whole recovered effect** on a throwaway copy of memory, runs the **whole original
ASM routine** on the real memory, and compares **every byte of memory** plus the
return control-flow (cs:ip:sp). Nothing can slip between two checks because the
two checks are "the complete machine state after the recovered routine" and "the
complete machine state after the ASM routine" — from the identical entry state.

It reports exactly *which hook* left *which memory regions* different (named to
their segment), so a divergence points straight at the culprit. Identical
divergences (same hook + same locations) are printed once even if the bytes change
every call, so a drifting counter doesn't flood the log.

Cost is dominated by one full-memory copy + one ``memcmp`` per hook call. The
``memcmp`` is Python's built-in ``bytes/bytearray ==`` (a single C ``memcmp`` at
memory bandwidth, ~0.1 ms for 1.25 MB); the common no-divergence case stops there.
Only on an actual mismatch do we scan to list the differing regions. Meant for
offline validation of a snapshot/scenario, not the normal hybrid runtime.
"""
from __future__ import annotations

import copy

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry

# Hooks whose non-verify path is intentionally an ASM pass-through (they cannot be
# a pure replacement — e.g. panel_copy is a vsync-paced loop): there is nothing to
# full-effect-diff because the "recovered" path *is* the ASM. Skip them.
_PASSTHROUGH: set[tuple[int, int]] = {(0x1030, 0x3054)}  # frame_panel_copy

# phys-range -> human name, for localising a diff. DGROUP is listed before the code
# segment so addresses in their overlap read as game data (the usual culprit).
_MEM_SEGMENTS = (
    (0x100000, 0x140000, "EGA planes"),
    (0x1A0F0, 0x2A0F0, "1A0F"),
    (0x10300, 0x20300, "1030"),
)
_MAX_REGIONS = 24      # cap regions listed per divergence
_GAP = 4               # merge diff runs separated by <= this many equal bytes
_MAX_BYTES = 16        # bytes of rec/asm shown per region


def _name(phys: int) -> str:
    for lo, hi, nm in _MEM_SEGMENTS:
        if lo <= phys < hi:
            return f"{nm}:{phys - lo:04X}"
    return f"phys:{phys:06X}"


def _diff_regions(rec: bytearray, asm: bytearray):
    """Return ``[(location_name, rec_hex, asm_hex), ...]`` for every differing region.

    Only called on a real mismatch (the equality check already failed), so the
    O(n) scan is off the hot path. Nearby diffs (<= ``_GAP`` equal bytes apart) are
    coalesced into one region for readability.
    """
    n = min(len(rec), len(asm))
    regions = []
    i = 0
    while i < n:
        if rec[i] == asm[i]:
            i += 1
            continue
        lo = i
        end = i + 1
        j = i + 1
        while j < n and (j - end) <= _GAP:
            if rec[j] != asm[j]:
                end = j + 1
            j += 1
        hi = min(end, lo + _MAX_BYTES)
        regions.append((_name(lo), bytes(rec[lo:hi]).hex(), bytes(asm[lo:hi]).hex()))
        if len(regions) >= _MAX_REGIONS:
            regions.append(("…", "more", "regions"))
            break
        i = end
    return regions


def enable_pre2_full_state_verify(rt, *, on_result=None, only=None, max_asm_steps=2_000_000):
    """Install the foolproof full-effect verify over every recovered hook.

    ``on_result(name, ok, reason)`` fires once per *distinct* divergence (same hook +
    same set of differing locations is reported once; repeats are counted silently).
    ``only`` optionally restricts to a set of ``(cs, ip)`` keys.
    """
    cpu = rt.cpu
    cpu.pre2_dos = rt.dos
    registry.install(cpu)
    cpu.pre2_verify_mode = False  # recovered runs on the full-effect path; the wrapper is the oracle
    seen: dict[tuple, int] = {}   # (name, locations) -> repeat count, for dedup

    recovered_handlers = dict(cpu.replacement_hooks)
    for key, recovered in recovered_handlers.items():
        if (only is not None and key not in only) or key in _PASSTHROUGH:
            continue
        name = cpu.hook_names.get(key, f"{key[0]:04X}:{key[1]:04X}")
        cpu.replacement_hooks[key] = _make_wrapper(recovered, name, on_result, max_asm_steps, seen)


def _make_wrapper(recovered, name, on_result, max_asm_steps, seen):
    def wrapper(c):
        mem = c.mem
        entry = copy.copy(c.s)
        entry_cs, entry_sp = entry.cs, entry.sp
        ret_ip = mem.rw(entry.ss, entry_sp)              # near-ret target
        ret_sp = (entry_sp + 2) & 0xFFFF
        real_data = mem.data

        # --- run the WHOLE recovered effect on a throwaway copy (one copy) ---
        scratch = bytearray(real_data)
        mem.data = scratch
        c.s = copy.copy(entry)
        try:
            recovered(c)
        except Exception as exc:  # a recovery gap / crash IS a divergence
            mem.data = real_data
            c.s = copy.copy(entry)
            _drive_asm(c, entry_cs, ret_ip, ret_sp, max_asm_steps)
            _emit(on_result, seen, name, ("raised",), f"recovered raised {type(exc).__name__}: {exc}")
            return
        rec_returned = (c.s.cs == entry_cs and c.s.ip == ret_ip and c.s.sp == ret_sp)
        rec_regs = copy.copy(c.s)

        # --- run the WHOLE ASM routine on the real memory (authoritative) ---
        mem.data = real_data
        c.s = copy.copy(entry)
        if not rec_returned:
            interpret_current_instruction_without_hook(c)  # pass-through hook: nothing to diff
            return
        _drive_asm(c, entry_cs, ret_ip, ret_sp, max_asm_steps)

        # Neutralise the routine's freed stack frame (below entry SP in SS): the
        # recovered (Python stack) and ASM (8086 stack) legitimately leave different
        # leftovers there, and it is don't-care once the routine has returned.
        ss_lo = (entry.ss << 4) & 0xFFFFF
        scratch[ss_lo:ss_lo + entry_sp] = real_data[ss_lo:ss_lo + entry_sp]

        # --- compare: fast path is one memcmp over all of memory ---
        if scratch != real_data:                          # equal -> done (common case)
            regions = _diff_regions(scratch, real_data)
            locs = tuple(loc for loc, _, _ in regions)
            reason = " ".join(f"[{loc} rec={a} asm={b}]" for loc, a, b in regions)
            _emit(on_result, seen, name, locs, reason)
        elif (rec_regs.cs, rec_regs.ip, rec_regs.sp) != (c.s.cs, c.s.ip, c.s.sp):
            _emit(on_result, seen, name, ("ctrl",),
                  f"control-flow rec=(cs={rec_regs.cs:04X} ip={rec_regs.ip:04X} sp={rec_regs.sp:04X}) "
                  f"asm=(cs={c.s.cs:04X} ip={c.s.ip:04X} sp={c.s.sp:04X})")
        elif on_result is not None:
            on_result(name, True, None)                   # match
    return wrapper


def _emit(on_result, seen, name, locs, reason) -> None:
    """Report a divergence once per (hook, differing-locations); count repeats silently."""
    sig = (name, locs)
    if sig in seen:
        seen[sig] += 1
        return
    seen[sig] = 1
    if on_result is not None:
        on_result(name, False, reason)


def _drive_asm(c, entry_cs, ret_ip, ret_sp, max_asm_steps):
    """Single-step the original ASM routine until it returns to its caller.

    No async IRQs are injected (the recovered effect is also synchronous), so the
    stack unwinds cleanly and ``sp``/``ip`` identify the return precisely.
    """
    steps = 0
    while not (c.s.cs == entry_cs and c.s.ip == ret_ip and c.s.sp == ret_sp):
        interpret_current_instruction_without_hook(c)
        steps += 1
        if steps > max_asm_steps:
            break
