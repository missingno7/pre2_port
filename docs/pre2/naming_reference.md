# Naming reference: cyxx/blues `p2` → our offsets

[cyxx/blues](https://github.com/cyxx/blues/tree/master/p2) is an independent, from-scratch C reimplementation of
Prehistorik 2. It is **not byte-accurate** (different struct layout, native pointers, `bool`s), so we take **names
and semantics from it, never offsets** — we already have our offsets from the DOS RE; names were the only gap.

This turns the object-model dissolve from "reverse-engineer the meaning of these bytes" into "apply a known
name," and shrinks placeholder names to the few fields cyxx *itself* marks `unk`. The object counts match
one-for-one (12 monsters, 4 projectiles, 32 bonuses, 16 score displays), which is strong evidence the model
transfers.

> License note: field *names* are facts (not copyrightable) and safe to use as a reference. Do **not** vendor or
> paste cyxx source into this repo — cross-reference only. Verify the repo's license before copying anything.

## A. What this surfaced: dual-use (UNION) offsets — do NOT blind-rename

Investigating the apparent "corrections" revealed that the top ones are **dual-use offsets**: the DOS game reuses
the same byte(s) for different purposes across code paths (a classic tight-memory technique). Our per-call-site
names are each ASM-evidence-based and capture the LOCAL meaning — which is *more* informative than a single global
name. cyxx, being a clean reimplementation, split or picked one canonical meaning. So cyxx is a **cross-reference /
disambiguation aid**, NOT a find-replace source. Verified unions:

| offset | our name(s) — each ASM-cited | cyxx canonical | verdict |
|---|---|---|---|
| **0x6EA9** | `WALL_MARKER_LIST` (collision, asm 64FA) **and** the firefly swarm slots (asm 54AB) — same 0x55AA-dead sentinel | `fly_tbl` | UNION — keep both; region reused per level |
| **0x6BC9** | `HURT_COOLDOWN` (combat spawn, asm 8276) **and** `BONUS_ENERGY_CTR` (pickup, asm 868E) | `bonus_energy_counter` | UNION — keep both per-context names |
| `Motion.low_gravity` @ 0x6BC7 | glide/float gate (asm) | `player_gravity_flag` (0/1/2) | cyxx clarifies it's tri-state; note in comment, keep field |
| cluster `CameraScript` @ 0xA3F7+ | camera-script engine (asm 71BF/7203/7290 script entries; TARGET_A/B camera-target ptrs) | gorilla `boss` `obj1/2/3`, `hdir`, `x_dist` | RESOLVED = UNION. Our ASM shows a GENERAL camera-script/target state machine; cyxx's `boss` reuses the same bytes on the level-9 gorilla fight. Keep our names; cyxx is the level-9 view. |
| `Scroll.to_dark/to_light/lights_off` @ 0x6C01/2/4 | palette day/night fields | `light {palette_flag1/2,state}` | matches — safe to note; the cluster mixes light + scroll |

**Rule going forward:** cyxx names are a reference in [naming_reference.md](naming_reference.md), added as
cross-ref *comments*, not blind renames. Only rename where a field is genuinely un-named/placeholdered on our side
(no evidence-based name exists yet), or where a single meaning is unambiguous and un-conflicted.

## B. Globals: `vars_t` → our clusters

Exact / high-confidence:

| cyxx `vars_t` field | our name | offset |
|---|---|---|
| `random {a,b,c,d,e}` | `Rng {lcg_a,lcg_b,lcg_c,lcg_d,ror}` | 0x2CEC.. |
| `input.key_up/down/left/right` | `Input {up,down,left,right}` | 0x27EA/EB/ED/EC |
| `input.key_space` / `key_jump` | `Input.fire` / (jump) | 0x27E8 |
| `input.key_hdir` / `key_vdir` | `PlayerState.input_lr` / `input_ud` | 0x6BDB/DC |
| `input.demo_offset/mask/counter` | demo-input buffer / `input_scratch` | 0x287A.. |
| `level_num` | `Progress.level` | 0x2D8A |
| `score` | `Progress.score_lo/hi` | 0x6C0E/10 |
| `player_lifes` / `player_energy` | `Progress.lives` / `energy` | 0x27D8 / 0x27D6 |
| `player_bonus_letters_mask` / `player_utensils_mask` | `Progress.bonus_letters` / `utensils_mask` | 0x6CA7 / 0x6CA8 |
| `player_gravity_flag` | `Motion.low_gravity` | 0x6BC7 |
| `player_flying_flag/counter` | `PlayerState.glider` / `Motion.fly_timer` | 0x6BC5 / 0x6BC8 |
| `bonus_energy_counter` | `Motion.hurt_cooldown` | 0x6BC9 |
| `level_current_bonuses_count` | `SceneryState.collected_counter` | 0x2A76 |
| `level_current_secrets_count` | `SceneryState.collected_linked` | 0x2A7A |
| `level_items_count_tbl[140]` | `item_collect_state` buffer (0x6C12..0x6CA0 ≈ 142B) | 0x6C12 |
| `level_bonuses_count_tbl[80]` | `bonus_cell_dedup` buffer (80B) | 0xA2A8 |
| `decor_tile0_offset` | `SceneryState.dipping_tile` | 0x6BAB |
| `current_hit_object` | `SceneryState.current_object` | 0x6BB1 |
| `shake_screen_counter` | `PlayerState.camera_shake` | 0x6BEA |
| `level_animated_tiles_counter` | render counter `ANIM_REMAP_THRESH` | 0x6BD4 |
| `tilemap.x/y` | `Camera.col/row` | 0x2DE4/E6 |
| `tilemap.redraw_flag1/flag2` | `SceneryState.page_dirty` / `grid_dirty*` | 0x6BBD / 0x2DE0 |
| `monster.hit_mask` / `collide_y_dist` | `HitScratch.hit_flag` / `hit_detail` | 0xA330 / 0xA331 |
| `current_bonus {x_pos,y_pos,spr_num}` | `SpawnCursor.burst_x/burst_y/burst_sprite` | 0xA336/38/3A |
| `light {state,palette_flag1/2,counter}` | `Scroll.lights_off/to_dark/to_light` | 0x6C04/01/02 |
| `boss.hdir` / `x_dist` | `CameraScript.dist_dir` / `dist_x` | 0xA3FA / 0xA3FB |
| `boss.obj1/obj2/obj3` | `CameraScript.target_a/target_b/cam_target_ptr` | 0xA423/25/21 |

To locate / not-yet-named on our side: `score_extra_life`, `level_draw_counter`, `player_runup_counter`,
`player_moving_counter`, `player_club_type/power/anim_duration` (→ `AttackState`), `current_platform_dx/dy`,
`snow {value,counter,pattern,random_tbl[256]}` (weather), `orb_tbl[20]` (spider webs), `panel {...}` (HUD copy),
`boss_level5` (tree), `boss_level9` (minotaur → our `BossScript` @ 0xA517).

## C. The object record: `object_t` union → our `ObjectSlot` / `EffectSlot`

cyxx's `object_t` is a common header + a per-type **union** — this is exactly the arena/pool record body we were
going to placeholder:

```
object_t = { x_pos, y_pos, spr_num, x_velocity, x_friction, <union>, hit_counter }
  union data:
    player_t          = { hdir, current_anim_num, anim*, y_velocity, special_anim_num }
    club_projectile_t = { anim*, y_velocity }
    monster_t         = { flags, ref*, x_velocity, y_velocity, anim*, state, energy, hit_jump_counter }
    thing_t           = { ref*, counter, y_velocity }
```

So a record's body bytes past the header are **named per object type** — `monster_t` gives us
`flags / state / energy / hit_jump_counter` for the enemy body (our `Actor.state/hp/hits` etc.), and names the
two pointers (`ref`, `anim`). This is the naming source for `object_inject`'s arena bodies: pick the union arm by
handler/type, apply the field names, placeholder only what cyxx leaves `unk`.

## C2. Confirmed record identities (fetched `game.h` + `resource.h`, 2026-07-14)

Mining the actual headers pinned two of our record views to cyxx's **level-load** structs by exact count match —
strong, independent confirmation, and it named our placeholders:

| our record | cyxx struct | count match | field mapping |
|---|---|---|---|
| `EffectSource` @ 0x8F1D, stride 7 | **`level_item_t`** | `SRC_COUNT 70` == `MAX_LEVEL_ITEMS 70` | x→x_pos, y→y_pos, sprite→spr_num, **bounce→y_delta** |
| `BonusCell` @ 0x8C8D, stride 5 | **`level_bonus_t`** | `count 80` == `MAX_LEVEL_BONUSES 80` | **reserved0/1/2 → tile_num0/tile_num1/count**, cell→pos |

Applied: `BonusCell` placeholders `reserved0/1/2` renamed to `tile_num0/tile_num1/count` (byte-exact — never
referenced by logic); `EffectSource` + `BonusCell` docstrings cross-ref cyxx. Other level-load structs to leverage
when we name the loader's tables: `level_gate_t[20]`, `level_column_t[15]`, `level_platform_t[16]`,
`level_monster_t[150]` (the level's monster spawn table: len/type/spr_num/flags/energy/respawn_ticks/score/x/y).

## C3. `object_t` index ranges (the unified objects_tbl → our separate pools)

cyxx keeps ONE `objects_tbl[OBJECTS_COUNT]`; the union arm is chosen by the object's INDEX. That index map tells
us which pool is which type:

| cyxx objects_tbl index | union arm | our pool |
|---|---|---|
| `objects[1]` | `player_t` | the player slot (0x4F1C, render slot #1) |
| `objects[2..5]` | `club_projectile_t` | `projectiles[4]` @ 0x4F2E |
| `objects[11..22]` | `monster_t` | `actors[12]` @ 0x4FD0 |
| `objects[23..74]` | `thing_t` | the effect/thing pools |

So our `actors` pool IS the **monster** arm — `Actor.state/hp/hits` = `monster_t.state/energy/hit_jump_counter`,
and `Actor.def_ptr` = `monster_t.ref` (the pointer this record follows). `thing_t` = `{ref, counter, y_velocity}`.

## D. Pools: object counts (confirms our layout)

| cyxx count | our pool | base |
|---|---|---|
| 12 monsters | `actors[12]` | 0x4FD0 |
| 4 projectiles | `projectiles[4]` | 0x4F2E |
| 32 bonuses | `bursts[32]` | 0x50A8 |
| 16 score displays | `debris[16]` | 0x5450 |
| 5 hitting frames | `popup_ring[5]` | 0x4F76 |

## E. The pointer kernel — now with known swizzle targets

Every pointer cyxx names tells us what our stored 16-bit offset should become in a real object model:

| cyxx pointer (type) | our field(s) | swizzle to |
|---|---|---|
| `boss.obj1/obj2/obj3` (`object_t*`) | `target_a/target_b/cam_target_ptr` | **index into the objects array** |
| `monster.ref` (`void*`) | linked-entity field (record +9) | object index |
| `current_hit_object` (`object_t*`) | `current_object` 0x6BB1 | object index |
| `*.anim` / `current_anim` / `boss_level9.seq` | anim/script cursors | **index/offset into the anim-script asset** |
| `level_animated_tiles_current_tbl` | `ANIM_REMAP_PTR` 0x6BC2 | index into the tile-state tables |
| `snow.pattern` | (weather) | index into the pattern asset |

The shipped object model stores the **reference/index**; the detachable bridge serializer swizzles ref↔offset to
reproduce a byte-exact DOS image for verification. No offset ever appears in shipped code.

## F. Read-only tables (cyxx `staticres.c` names)

`score_tbl[17]` (→ our `SCORE_TABLE`), `score_spr_lut[110]` (→ `SCORE_SPR_LUT`), `player_anim_lut[32]`
(→ player anim height/lut), `monster_spr_tbl[48]`, `boss_gorilla_data[190]`, `boss_gorilla_spr_tbl[138]`,
`spr_offs_tbl[922]` / `spr_size_tbl[922]` (sprite geometry), `palettes_tbl[16]`. These are loaded input → in the
object model they become **named, index-addressed loaded arrays**, not offset reads.

## Worklist (revised after the union finding)

1. **Cross-ref comments, not renames** (§A) — where cyxx and our name differ on a *union* offset, add the cyxx
   name as a `# cyxx: <name>` comment; keep our per-context ASM-evidence names. NO wholesale renames.
2. **Fill genuinely-missing globals** (§B "to locate") — for fields we never named (score_extra_life,
   player_club_*, current_platform_dx/dy, orb_tbl, snow, panel, ...), adopt cyxx's names directly; no conflict.
3. **Arena bodies** (§C) — apply the `object_t` union names in `object_inject`; placeholder only cyxx's own
   `unk`s. This is where the naming reference pays off most (it was the hardest un-reversed tier).
4. **Pointer swizzle** (§E) — the design step: store refs/indices, teach the bridge serializer to swizzle.
5. **Loaded tables** (§F) — name + index-address them.

**Key lesson:** the DOS build reuses bytes across code paths (unions). Our per-call-site names are a feature, not
a bug — cyxx's single canonical name can lose the local meaning. Treat cyxx as authoritative for *structure and
un-named fields*, and as a cross-reference (not an override) for offsets we already name from ASM evidence.
