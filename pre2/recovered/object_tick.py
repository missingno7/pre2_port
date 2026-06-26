"""Prehistorik 2 object-update walker — the COMPOSED high-level tick (recovered, pure).

This is the top of the object-system island: a single `object_tick` that reproduces the per-frame walker
`1030:684E..6913` by composing the already byte-verified leaves (`pre2/recovered/object_update.py`) instead of
hooking each one. Per the disasm, one pass walks 12 slots of the 18-byte record list at `0x4FD0` and, for each
non-empty slot (`[si+4]!=0xFFFF`):

    apply_velocity  →  (terrain_collision if [def+4]&8)  →  advance_animation  →  AI handler (cs:[idx*2+0x6AA9])

The handler is dispatched through :data:`HANDLERS` (handler-address → recovered function). All 12 *witnessed*
types are recovered; an unrecovered handler or the non-zero-scale anim path raises :class:`Pre2ObjectGap`
(fail loud — never silently fall back to the ASM).

`object_tick` is VM-independent: it talks to memory only through a :class:`WalkerMem` accessor (so it can run
live against the VM, or dict-backed in tests). It does NOT model object SPAWNING (the `7FD9`/`7DE6` effect
emitters that some handlers call) — those touch *other* slots / effect state, never the acting object's own
record, so per-slot behaviour stays byte-exact; a full cross-slot whole-memory reproduction additionally needs
those emitters (the next recovery target)."""
from __future__ import annotations

from pre2.recovered.object_update import (ObjectScaleUnsupported, advance_animation, apply_velocity,
                                          handle_object_75c4, handle_object_760f, handle_object_7665,
                                          handle_object_773d, handle_object_77de, handle_object_7898,
                                          handle_object_78ec, handle_object_7a60, handle_object_7adf,
                                          handle_object_7b91, handle_object_7c2d, handle_object_7c8c,
                                          handle_object_7c90, terrain_collision)

__all__ = ["object_tick", "HANDLERS", "Pre2ObjectGap", "OBJ_BASE", "OBJ_STRIDE", "OBJ_COUNT"]

OBJ_BASE = 0x4FD0      # [asm 684E] base of the 12-slot object record list
OBJ_STRIDE = 0x12      # [asm 690A] 18-byte records
OBJ_COUNT = 12         # [asm 6851 bp=0xC]

# handler-address (cs:[idx*2 + 0x6AA9]) -> recovered AI handler. All witnessed types (0-12).
HANDLERS = {0x7C90: handle_object_7c90, 0x7C8C: handle_object_7c8c, 0x7C2D: handle_object_7c2d,
            0x7B91: handle_object_7b91, 0x7ADF: handle_object_7adf, 0x7A60: handle_object_7a60,
            0x7898: handle_object_7898, 0x78EC: handle_object_78ec, 0x77DE: handle_object_77de,
            0x773D: handle_object_773d, 0x7665: handle_object_7665, 0x760F: handle_object_760f,
            0x75C4: handle_object_75c4}


class Pre2ObjectGap(Exception):
    """The walker reached unrecovered object behaviour (an unrecovered AI handler, or the non-zero-scale
    boss-zoom anim remap). Fail loud rather than silently running the original ASM."""


def _obj_view(mem, si):
    return {"x": mem.rw(si), "y": mem.rw(si + 2), "id": mem.rw(si + 4), "xvel": mem.rw(si + 8),
            "yvel": mem.rw(si + 0xA), "anim_ptr": mem.rw(si + 0xC), "state": mem.rb(si + 0xE)}


def _write_obj(mem, si, o):
    mem.ww(si, o["x"]); mem.ww(si + 2, o["y"]); mem.ww(si + 4, o["id"]); mem.ww(si + 8, o["xvel"])
    mem.ww(si + 0xA, o["yvel"]); mem.ww(si + 0xC, o["anim_ptr"]); mem.wb(si + 0xE, o["state"])


# def-record fields: name -> (offset, is_word). The same offset is a type-specific union (see object_update),
# so it is always read as bytes and the handlers reconstruct words where needed.
_DEF_BYTES = (("d4", 4), ("d6", 6), ("d7", 7), ("dD", 0xD), ("dE", 0xE), ("dF", 0xF), ("d10", 0x10),
              ("d11", 0x11), ("d12", 0x12), ("d13", 0x13), ("d14", 0x14))
_DEF_WORDS = (("d2", 2), ("d9", 9), ("dB", 0xB))


def _def_view(mem, d):
    o = {k: mem.rb(d + off) for k, off in _DEF_BYTES}
    o.update({k: mem.rw(d + off) for k, off in _DEF_WORDS})
    return o


def _write_def(mem, d, df):
    for k, off in _DEF_BYTES:
        mem.wb(d + off, df[k])
    for k, off in _DEF_WORDS:
        mem.ww(d + off, df[k])


def _dispatch(tgt, fn, o, df, glb, mem):
    """Call one AI handler with its type-specific callbacks (the same wiring as the shadow probe).

    The spawning handlers (idx2/3/4) emit trail/effect entries into the secondary effect list (``0x7DE6``)
    via ``mem.spawn(def9, defB, arg, dl)`` -> ``spawn_effects``; ``mem`` without a ``spawn`` (e.g. dict-backed
    tests) gets ``spawn=None`` (no emit), which never touches the acting object's own record."""
    spawn = getattr(mem, "spawn", None)
    if tgt == 0x7B91:                                  # idx3 reads the level-map terrain property + spawns
        fn(o, df, glb, mem.rw, tile_prop=mem.tile_prop, spawn=spawn)
    elif tgt == 0x7ADF:                                # idx4 reads the cos/sin tables + spawns
        fn(o, df, glb, mem.rw, cos_table=mem.cos_table, sin_table=mem.sin_table, spawn=spawn)
    elif tgt == 0x7C2D:                                # idx2 vertical-bob spawns trail effects
        fn(o, df, glb, mem.rw, spawn=spawn)
    else:
        fn(o, df, glb, mem.rw)


def object_tick(mem) -> None:
    """Run one full object-update pass (`1030:684E..6913`) over the 12 slots, mutating `mem` in place.

    `mem` is a :class:`WalkerMem`-style accessor: word/byte read+write in the object data segment (`rw/rb/ww/
    wb`), the map terrain lookup `tile_prop(tx,ty)` + raw `read_map`/`prop_a`/`prop_b`/`slope`, the cos/sin
    tables, the global-scale `scale`, the handler-address lookup `handler_addr(idx)`, and a `glb` dict factory.
    """
    for slot in range(OBJ_COUNT):                                # [asm 6851/690D loop over 12 slots]
        si = OBJ_BASE + slot * OBJ_STRIDE
        if mem.rw(si + 4) == 0xFFFF:                             # [asm 6856-685E] empty slot -> skip
            continue
        nx, ny = apply_velocity(mem.rw(si), mem.rw(si + 2), mem.rw(si + 8), mem.rw(si + 0xA))   # [6861-6873]
        mem.ww(si, nx); mem.ww(si + 2, ny)
        d = mem.rw(si + 6)
        if mem.rb(d + 4) & 8:                                    # [6875-687E] terrain collision gate
            o = {"x": mem.rw(si), "y": mem.rw(si + 2), "xvel": mem.rw(si + 8), "yvel": mem.rw(si + 0xA),
                 "anim_ptr": mem.rw(si + 0xC)}
            df = {"d4": mem.rb(d + 4)}
            terrain_collision(o, df, mem.read_map, mem.prop_a, mem.prop_b, mem.slope, mem.rw)
            mem.ww(si, o["x"]); mem.ww(si + 2, o["y"]); mem.ww(si + 8, o["xvel"])
            mem.ww(si + 0xA, o["yvel"]); mem.ww(si + 0xC, o["anim_ptr"]); mem.wb(d + 4, df["d4"])
        try:                                                     # [6881-68E6] animation advance
            anim = advance_animation(mem.rw(si + 0xC), mem.rw, mem.rw(si + 4), mem.rb(si + 9), mem.scale())
        except ObjectScaleUnsupported as e:
            raise Pre2ObjectGap(f"slot {slot}: {e}") from e
        mem.ww(si + 4, anim.sprite_id); mem.ww(si + 0xC, anim.script_ptr); mem.wb(0xA340, anim.attr_a340)

        idx = mem.rb(d + 1)                                      # [68EC] mov bl,[bx+1]
        tbl = (idx << 1) & 0xFF                                  # [68EF-68F1] xor bh,bh ; shl bl,1 (8-BIT: the
        tgt = mem.handler_addr(tbl)                              # 0x80 bit shifts out — it is a flag, not idx)
        fn = HANDLERS.get(tgt)                                   # [68FC] cs:[bx + 0x6AA9]
        if fn is None:
            raise Pre2ObjectGap(f"slot {slot}: unrecovered handler idx {idx & 0x7F} @ {tgt:#06x}")
        o, df, glb = _obj_view(mem, si), _def_view(mem, d), mem.glb()
        _dispatch(tgt, fn, o, df, glb, mem)
        _write_obj(mem, si, o); _write_def(mem, d, df)
        if tgt == 0x78EC:                                        # idx6 also writes the global shake + PRNG state
            mem.write_glb(glb)
