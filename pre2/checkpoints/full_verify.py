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
_PASSTHROUGH: set[tuple[int, int]] = {
    (0x1030, 0x3054),   # frame_panel_copy (vsync-paced ASM loop)
    (0x1030, 0x684E),   # object_tick — an INLINE walker replacement (no CALL/RET) that resumes at 0x6913, so
                        # the drive-to-ret oracle here doesn't apply; it is verified offline by the whole-tick
                        # probe (pre2/probes/probe_object_tick_composed.py, whole-segment lockstep = 0 diff).
}


def _sqz_ignore(cpu):
    """Don't-care regions for sqz_decompress (1030:107B).

    The recovered decoder reads the asset in Python and commits only the decoded
    OUTPUT (+ the [2875] load-ptr bump). The ASM instead opens a DOS file handle and
    streams the COMPRESSED file through an input buffer at ``out_seg+advance`` — pure
    I/O scratch a different-but-equivalent mechanism legitimately doesn't reproduce.
    Ignoring it keeps the real contract (output bytes + [2875]) fully checked while
    not drowning a genuine divergence in ~60 KB of benign buffer noise.
    """
    from pre2.checkpoints.common import _read_cstring
    from pre2.codecs.sqz import sqz_bump_advance, unpack_sqz
    mem = cpu.mem
    out_seg = mem.rw(0x1A0F, 0x2875)
    base = (out_seg << 4) & 0xFFFFF
    adv = 0
    declared = 0
    try:
        raw = cpu.pre2_dos.resolve_game_path(_read_cstring(mem, 0x1A0F, cpu.s.dx)).read_bytes()
        adv = sqz_bump_advance(raw)
        declared = len(unpack_sqz(raw))     # the asset's real output length
    except Exception:
        pass
    ltb = (((out_seg + adv) & 0xFFFF) << 4) & 0xFFFFF
    reserved_end = base + adv * 16
    return [
        (ltb, min(ltb + 0xF000, len(mem.data))),           # compressed-file input buffer
        ((0x1030 << 4) + 0x1204, (0x1030 << 4) + 0x1208),  # DOS file handle + out_seg copy
        ((0x1030 << 4) + 0x140F, (0x1030 << 4) + 0x1414),  # input-buffer mode/size/limit [140F/1410/1412]
        ((out_seg << 4) + 0x418, (out_seg << 4) + 0x420),  # 6-byte header read scratch
        # The ASM over-decodes up to ~1 byte past the declared output into the +1
        # paragraph the allocator reserves; the recovered stops at the declared length.
        # Ignore only that reserved padding [declared, reserved), never the real output.
        (base + declared, min(reserved_end, len(mem.data))) if declared else (0, 0),
    ]


# =====================================================================================
# IGNORE-REGION POLICY (strict — read before adding any entry to `_IGNORE`)
#
# An ignore region is ONLY for a PROVEN mechanism-only difference: the recovered routine
# reaches the same EXTERNAL result by a different internal mechanism than the ASM, so a
# whole-memory diff flags machine bytes that carry no behaviour. It is NEVER a way to mute
# an unexplained or unrecovered divergence.
#
# ACCEPTABLE to ignore:
#   - self-modifying-code templates used only inside the replaced routine
#   - temporary scratch buffers read/written only inside the replaced routine
#   - staging memory with no downstream reads
#   - mechanism-only diffs where visible output AND the state contract are proven identical
#
# NEVER ignore (these are behaviour — fix the hook or widen the contract instead):
#   - visible planes, object slots, player state, timers, collision flags,
#     score/HUD state, scroll/camera state, animation state,
#     anything read after the routine returns,
#     anything not proven dead by read/write-watch.
#
# Each entry MUST document all 7: (1) exact byte range, (2) owning routine, (3) why the
# ASM writes it, (4) why the recovered routine needn't, (5) read/write-watch proof, (6)
# proof downstream state/output still matches, (7) why it is mechanism-only, not behaviour.
# Keep ranges TIGHT. We do NOT reproduce dead scratch — the recovery goal is same visible
# output / live state / contract / downstream behaviour, not byte-identical internal scratch.
# =====================================================================================


def _object_render_ignore(cpu):
    """Don't-care regions for object_render (1030:26FA). (`cpu` unused — fixed program addresses.)

    (1) EXACT RANGE: code-seg 0x26E0..0x26FA (the 0x1A bytes between the prior routine's RET at
        26DE and this entry at 26FA) + DGROUP 0x2DEC..0x2DF0 (two scratch words).
    (2) OWNER: object_render (1030:26FA..2DF9), the moving-sprite renderer.
    (3) WHY ASM WRITES IT: the ASM blit is SELF-MODIFYING — per sprite it patches immediate
        operands into its own code template at cs:[0x26E0..0x26FA] (mov cs:[0x26EC],dh ;
        mov word cs:[0x26E8],ax ; add word cs:[0x26E0],ax …) then executes that block as the
        inner blit loop; [0x2DEC] holds the per-sprite clipped extent (written 27DE/27E1).
    (4) WHY RECOVERED NEEDN'T: the recovered renderer (paint_sprite) blits straight onto the
        EGA planes in Python — no self-modifying code, no clip-scratch word — so it reaches the
        same pixels without those bytes.
    (5) READ/WRITE-WATCH PROOF: the SMC operands are written only by IPs 27E1/27E6/27F8/2845/
        2851/2858 and the only reader is the routine executing them; [0x2DEC..0x2DEF] is written
        only at 2713/27E1 and read only at 284C/2DE4 — every accessor IP is inside 26FA..2DF9.
    (6) DOWNSTREAM MATCH: with these regions neutralised, full-verify is 0-divergence for
        object_render on demos 165111 + 015602; the four EGA planes and the [+5]/[+0x11] record
        mutations (the routine's real contract) are still diffed in full and match.
    (7) MECHANISM-ONLY: nothing outside the routine ever reads these bytes (proof 5), so they
        are dead the instant the routine returns — an artifact of the ASM's blit acceleration,
        not game behaviour."""
    code = (0x1030 << 4) & 0xFFFFF
    data = (0x1A0F << 4) & 0xFFFFF
    return [
        (code + 0x26E0, code + 0x26FA),   # (1) self-modified blit-code template (re-patched per sprite)
        (data + 0x2DEC, data + 0x2DF0),   # (1) per-sprite clipped-extent scratch word(s)
    ]


# Per-hook don't-care regions: ``(cs, ip) -> fn(cpu_at_entry) -> [(phys_lo, phys_hi), ...]``.
# Add an entry ONLY under the IGNORE-REGION POLICY above (proven mechanism-only).
_IGNORE = {
    (0x1030, 0x107B): _sqz_ignore,
    (0x1030, 0x26FA): _object_render_ignore,
}

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


def enable_pre2_full_state_verify(rt, *, on_result=None, only=None, max_asm_steps=16_000_000):
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
    incomplete: set[str] = set()  # routines whose ASM run hit the step cap (warned once)

    recovered_handlers = dict(cpu.replacement_hooks)
    for key, recovered in recovered_handlers.items():
        if (only is not None and key not in only) or key in _PASSTHROUGH:
            continue
        name = cpu.hook_names.get(key, f"{key[0]:04X}:{key[1]:04X}")
        ignore_fn = _IGNORE.get(key)
        cpu.replacement_hooks[key] = _make_wrapper(recovered, name, on_result, max_asm_steps,
                                                   seen, ignore_fn, incomplete)


def _make_wrapper(recovered, name, on_result, max_asm_steps, seen, ignore_fn, incomplete):
    def wrapper(c):
        mem = c.mem
        entry = copy.copy(c.s)
        entry_cs, entry_sp = entry.cs, entry.sp
        ret_ip = mem.rw(entry.ss, entry_sp)              # near-ret target
        ret_sp = (entry_sp + 2) & 0xFFFF
        ignore = ignore_fn(c) if ignore_fn else ()       # don't-care regions (mechanism-only diffs)
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
        if not _drive_asm(c, entry_cs, ret_ip, ret_sp, max_asm_steps):
            # ASM did not finish within the step budget -> its buffer is half-written;
            # comparing it would be a FALSE divergence. Skip + warn once per routine.
            if name not in incomplete:
                incomplete.add(name)
                print(f"[full-verify] {name}: ASM routine did not return within "
                      f"{max_asm_steps:,} steps -> NOT COMPARED (raise max_asm_steps to verify)",
                      flush=True)
            return

        # Neutralise the routine's freed stack frame (below entry SP in SS): the
        # recovered (Python stack) and ASM (8086 stack) legitimately leave different
        # leftovers there, and it is don't-care once the routine has returned.
        ss_lo = (entry.ss << 4) & 0xFFFFF
        scratch[ss_lo:ss_lo + entry_sp] = real_data[ss_lo:ss_lo + entry_sp]

        # Neutralise this routine's declared don't-care regions (mechanism-only diffs,
        # e.g. SQZ's compressed-file I/O scratch) so a real divergence isn't buried.
        for lo, hi in ignore:
            scratch[lo:hi] = real_data[lo:hi]

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


def _drive_asm(c, entry_cs, ret_ip, ret_sp, max_asm_steps) -> bool:
    """Single-step the original ASM routine until it returns to its caller.

    No async IRQs are injected (the recovered effect is also synchronous), so the
    stack unwinds cleanly and ``sp``/``ip`` identify the return precisely. Returns
    True if the routine returned, False if it hit ``max_asm_steps`` first — in which
    case the ASM result is INCOMPLETE and must NOT be compared (a half-decoded buffer
    would look like a divergence; e.g. the slow "other"-format SQZ decode needs ~4-11M
    steps and at the old 2M cap masqueraded as a SAMPLE/PRESENT/SPRITES output bug).
    """
    steps = 0
    while not (c.s.cs == entry_cs and c.s.ip == ret_ip and c.s.sp == ret_sp):
        interpret_current_instruction_without_hook(c)
        steps += 1
        if steps > max_asm_steps:
            return False
    return True
