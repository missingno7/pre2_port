# Pointer swizzle: the last step to a fully offset-free object model

## Why this is the final tier

The name-frontier grind dissolved ~75% of gameplay-logic offset access into named record/array views. The
remaining raw is dominated by **stored pointers** — 16-bit DGROUP offsets held *as data* in the state, which the
game reads and dereferences. A pure offset-free backend can't resolve them, because the stored *value itself* is
an offset. A real game holds a **reference or index**; the DOS build holds a raw offset. Swizzling is how we
close that gap while keeping byte-exact verification.

## Principle

- The shipped object model (`pre2/game`) stores a **typed reference** in each pointer field — never an offset.
- Gameplay logic dereferences the reference naturally (`deref(obj.def_ptr).d4`, `objects[ref.index]`).
- The **detachable bridge** (`pre2/bridge`) owns the only offset↔reference map: its serializer swizzles a
  reference back into the exact DOS offset when producing a byte image, and offset→reference when parsing.
- **Invariant:** `offset → ref → offset` is the identity, so the serialized image stays byte-identical to the
  DOS original — `serialize→memcmp` verification is fully preserved.

## Pointer inventory (from our layouts + the naming reference)

| family | fields | points at | swizzle |
|---|---|---|---|
| **object-pool** | `ObjectSlot.def_ptr` (+6), `SceneryState.current_object` (0x6BB1), `SpawnCursor.spawned_ptr` (0xA33E), `CameraScript.cam_target_ptr/target_a/target_b` (0xA421/23/25), the arena projection back-ptr (`record[+6]=si`), `find_free` slot | a record in an object array (0x4FD0 actors, 0x50A8 bursts, the arena) | `ObjectRef(pool, index)` — offset = `pool.base + index*stride` |
| **script/asset cursor** | `ObjectSlot.anim_ptr` (+0xC), `CameraScript.script_ptr/script_cursor` (0xA401/0xA3FF), `BossScript.script_ptr/m9_ptr` (0xA517/0xA4F7), `ANIM_REMAP_PTR` (0x6BC2), `level_animated_tiles_current_tbl`, `snow.pattern` | a byte cursor into a loaded asset (anim script, camera script, tile table) | `CursorRef(asset_id, byte_off)` — offset = `asset.base + byte_off` |
| **ring/list head** | `PlayerState.trail_ring`/`POPUP_RING_PTR`/`SPARKLE_RING_PTR` (0x6BBE), `SCROLL_SCRIPT_PTR`, `calc_scroll_source` ring | a slot in a fixed ring, walked with wrap | `RingRef(ring_id, index)` |
| **sprite bank / far** | `SceneryState.sprite_bank_lo/hi` (0x8C89/0x8C8B) | a rebase base for entity sprite refs | `BankRef(bank_id)` (or keep as a loaded-asset index) |

`def_ptr` is the highest-value / cleanest first target: it points at a fixed-layout type-def record, dereferenced
every frame (`ObjectDef(mem, def_ptr)` today) — so it becomes `deref(slot.def_ptr)` returning an `ObjectDef`.

## Reference types (shipped, offset-free — `pre2/game/ref.py`)

```
ObjectRef(pool, index)     # pool ∈ {actors, bursts, arena, ...}; deref -> the dataclass instance
CursorRef(asset, byte_off) # asset ∈ {anim_script, cam_script, tile_tbl, ...}; deref -> a view at byte_off
RingRef(ring, index)
NullRef                    # the 0x0000 / 0xFFFF sentinels -> None
RawRef(value)              # OPAQUE fallback: a stale/un-reversed pointer; stores the raw 16-bit value verbatim
```

`RawRef` is the placeholder analog for pointers: a stored offset we have not yet reverse-engineered (or a *stale*
freed-slot pointer that is serialized but never dereferenced) round-trips its exact value with **no** semantic
claim — keeping byte-exactness while we defer meaning. This is what makes incremental swizzling safe.

## The swizzle (detachable — `pre2/bridge/pointer_layout.py`)

```
to_offset(ref)  ->  the exact 16-bit DGROUP offset      # ObjectRef -> base+index*stride; CursorRef -> base+off; NullRef -> sentinel; RawRef -> value
from_offset(v)  ->  the reference                        # classify v by which known region it lands in; else RawRef(v)
```

`from_offset` classifies a raw offset by the region it points into (each object pool / asset has a known
`[base, base+len)` in the bridge layout). Anything unclassified → `RawRef` (loud: logged, counted, a worklist
item). The bridge's existing serializer (`state_fields` / `game_layout`) calls `to_offset` when writing a pointer
field into the image, and `from_offset` when parsing — so no shipped code ever sees an offset.

## Hard cases (all handled by the ref carrying enough info)

- **NULL sentinels** (0x0000 / 0xFFFF): `NullRef` ↔ the sentinel. `def_ptr==0` etc.
- **Interior pointers** (a cursor advanced *past* a record header): `CursorRef` stores the byte offset within the
  asset, not just the record — reproduces the exact mid-record offset.
- **Cross-segment** (pointers into the loaded level `es`, not DGROUP): a distinct asset id; the swizzle uses that
  segment's base. Out of DGROUP scope but same mechanism.
- **Stale/freed pointers**: `RawRef` preserves the exact bytes; never dereferenced, always round-trips.
- **Dual-use (union) offsets** (see [naming_reference.md](naming_reference.md)): a field that is a pointer in one
  code path and a scalar in another keeps per-context handling; only the pointer-path reads swizzle.

## Verification (unchanged, still the strongest gate)

Every step stays gated by `serialize→memcmp` against the recorded VM: `scripts/verify_object_finish.py`
(1579 ticks digest-matched) + the corpus. Because `to_offset(from_offset(v)) == v` for every reachable pointer,
the reconstructed DGROUP is byte-identical — a wrong swizzle fails LOUD at the exact offset+tick.

## Incremental rollout (each gated byte-exact; `RawRef` covers the not-yet-done)

1. **`def_ptr`** — object-pool ref to the type-def record. Cleanest: fixed base+stride, dereferenced via the
   `ObjectDef` view already added. Prove `deref(slot.def_ptr).d4 == ObjectDef(mem, slot.def_ptr).d4`.
2. **`anim_ptr` / script cursors** — `CursorRef` into the anim/camera-script assets.
3. **object-pool refs** — `current_object`, `spawned_ptr`, `target_a/b`, arena back-ptr, `find_free`.
4. **rings** — `RingRef` for the popup/sparkle/scroll rings.
5. Everything still `RawRef` at the end is the honest, enumerated residue (loud, counted) — the final placeholder
   tier, resolvable later by naming its region.

## Scope boundary (explicit)

This makes the **gameplay state of record** fully offset-free and reference-based. It does NOT convert the
**renderer** to read objects — the render image stays a *materialized projection* rebuilt from the object graph
each frame (proven a byte-exact render buffer by `verify_object_render.py`), which is a framebuffer, not state of
record. Renderer-on-objects is a separate, much larger effort with no practical benefit and is deliberately out
of scope.
