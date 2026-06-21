"""Checkpoint for the sprite-sheet decode (1030:4316 local + 1030:4389 shared).

Recovered logic: ``pre2.recovered.sprite_decode``; data model: ``pre2.bridge.sprites``.
Merge target: the sprite/asset pipeline.

Two co-dependent routines that demultiplex the decompressed sprite sheet into the
planar VRAM cache at level load. They are replaced together: 4316 writes the
[0x25CA] index copy that 4389 consumes. Verify mode diffs the planar cache slots
each writes (plus 4316's data + register contract) against the ASM at the RET.
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import sprites as _spr
from pre2.recovered.sprite_decode import PIXEL_BASE

from .common import _BUMP_PTR, _DATA_SEG, report

# GOG build: code old+0x1F, ds offsets old+4 (data seg 1A0F via common).
_SPR_LOCAL_ENTRY = (0x1030, 0x4316)
_SPR_LOCAL_EXIT = (0x1030, 0x4388)
_SPR_SHARED_ENTRY = (0x1030, 0x4389)
_SPR_SHARED_EXIT = (0x1030, 0x43D1)
_VAR_MULT_STORE = 0x2CF5   # [0x2CF5] = the paragraph multiplier byte (4316)
_VAR_BANK_SELECT = 0x2D8A  # [0x2D8A] index into the multiplier table
_VAR_BANK_TABLE = 0x2D30   # [bank_select + 0x2D30] multiplier
_VAR_INDEX_COPY = 0x25CE   # 4316 copies the 256-entry index table here; 4389 reads it
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
    """Native replacement for the local sprite-sheet demux at 1030:4316."""
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
    """Native replacement for the shared/union sprite-sheet demux at 1030:4389."""
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


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hooks at the two demux RET sites."""

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
                report(stats, on_result, raise_on_divergence, f"sprite_decode_{kind}", reason)
            interpret_current_instruction_without_hook(c)  # original near-ret
        return _exit

    cpu.replacement_hooks[_SPR_LOCAL_EXIT] = _sprite_verify_exit("local")
    cpu.hook_names[_SPR_LOCAL_EXIT] = "sprite_verify_local"
    cpu.replacement_hooks[_SPR_SHARED_EXIT] = _sprite_verify_exit("shared")
    cpu.hook_names[_SPR_SHARED_EXIT] = "sprite_verify_shared"
