"""Checkpoint (verifier) for the sprite classifier (1030:4232).

Recovered logic: ``pre2.recovered.sprite_classify``. Merge target: the sprite
pipeline.

Verify-mode lockstep only: at ``4232`` entry it classifies a *copy* of the planar
sprite cache with the recovered :func:`classify_sprites`, and at the routine's RET
(``42AE``) it diffs the recovered type table + partial transparency masks against
the ASM's ``[0x4DF8]`` / ``[0x2DF8]``.

No hybrid *replacement* yet (deliberate): ``4232`` is a load-time-only routine that
runs in EGA read-mode-1 and the exact register / GC state it leaves at its RET is
not yet pinned under lockstep — that needs a menu→level-load demo (the gameplay
demos start past it). The pure logic is proven byte-exact by the committed
``tests/test_sprite_classify.py``; until a load-time demo is available the ASM keeps
producing the tables in hybrid play (no gap — the recovered blit consumes them
either way). Promote to a replacement adapter + VERIFIED once this verify path runs
green over a real level load.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from pre2.bridge import frame as _frame
from pre2.bridge import sprites as _spr
from pre2.recovered.sprite_classify import FIRST_PARTIAL_ID, classify_sprites
from pre2.recovered.sprite_decode import SLOT_BYTES

from .common import report

_ENTRY = (0x1030, 0x4232)
_EXIT = (0x1030, 0x42AE)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep entry (classify a cache copy) + exit (diff) hooks."""

    def _entry(c) -> None:
        # 4232 only reads the cache, so the entry snapshot is its faithful input.
        cache = _spr.read_sprite_cache(c.mem)
        c.pre2_classify_pending.append(classify_sprites(cache))
        interpret_current_instruction_without_hook(c)

    def _exit(c) -> None:
        if c.pre2_classify_pending:
            res = c.pre2_classify_pending.pop()
            asm_types = _frame.read_blit_type_table(c.mem)   # [0x4DF8] 256 bytes
            reason = None
            if bytes(res.types) != asm_types:
                i = next(k for k in range(len(res.types)) if res.types[k] != asm_types[k])
                reason = f"type[{i}]: asm={asm_types[i]} rec={res.types[i]}"
            else:
                n = res.partial_count * SLOT_BYTES               # only the compacted partial masks
                asm_masks = _frame.read_mask_region(c.mem)[:n]    # [0x2DF8]
                if bytes(res.masks[:n]) != asm_masks:
                    j = next(k for k in range(n) if res.masks[k] != asm_masks[k])
                    pid = j // SLOT_BYTES + FIRST_PARTIAL_ID
                    reason = f"mask[{j}] (partial id {pid}): asm={asm_masks[j]:02X} rec={res.masks[j]:02X}"
            report(stats, on_result, raise_on_divergence, "sprite_classify", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_ENTRY] = _entry
    cpu.hook_names[_ENTRY] = "sprite_classify_entry"
    cpu.replacement_hooks[_EXIT] = _exit
    cpu.hook_names[_EXIT] = "sprite_classify_verify"
