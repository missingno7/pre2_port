"""The DOS byte layout of the game model — the offset-aware CONSTRUCTION of the object graph.

``pre2/game/model.py`` is the clean, offset-free game model; ``pre2/native/object_state.py``'s
:class:`ObjectGraphStore` is the offset-free STORAGE that runs it. This module is the third piece: it parses a
DGROUP byte image into those dataclasses (+ the byte-compat map the store routes ``rb``/``wb`` through) and
folds them back (:meth:`DataclassBackend.materialize`).

**Why this lives in ``pre2/native`` and not the bridge** (moved 2026-07-16, Stage 2.5 of
docs/pre2/offset_free_release_plan.md): the product must be able to BUILD its own object graph at boot without
importing ``pre2.bridge`` (scripts/lint.py forbids it — "the workbench is detachable; it imports the product,
never the reverse"). Until that was true, the object graph could only ever run inside verification scripts, so
the shipped default stayed the byte image and every per-module conversion was unable to change what the
deployed product actually executes. ``pre2/bridge/game_layout.py`` now imports these names and keeps only the
verification-only helpers (``player_from_image``/``rng_from_image``/``globals_field_routing``/
``NamedImageBackend``).

The offsets here are DATA, not an access pattern: no gameplay logic reads them, and they are the migration aid
:class:`ObjectGraphStore` needs only while un-converted callers still reach state via ``rb``/``rw``. They retire
with the last of those callers (P5) — see the module's own "byte-compatibility shim" note. Precedent for a
shipped offset-carrying data module: ``pre2/native/dgroup_offsets.py``.

A layout is ``(field, rel_offset, width, signed)`` per canonical field; the alias bytes (``flags``,
``facing_lo``, ``life``) are re-projections of a canonical field's bytes, so writing the canonical fields
reproduces them exactly — no separate entries needed. Evidence for each offset lives with the ``dgroup_view``
descriptors (the recovery spec); this table is the machine-readable serialisation layout.
"""
from __future__ import annotations

from pre2.game.model import (Actor, ArenaEntity, AttackState, AttractState, Boss, BonusCell, BossScript,
                             LevelTables,
                             ByteBuffer, Camera, CameraScript, DifficultyMode, EffectSlot, HitScratch, Input,
                             LevelState, Motion, Player, PlayerState, Progress, Rng, SceneryState, Scroll,
                             SpawnCursor, WallMarker)
from pre2.native.object_state import ObjectGraphStore

# named working-memory buffers the tick scribbles as raw bytes: (attr, base, length). Modeled as a bytearray,
# not fields (transient scratch, not records). These finish draining the mutable state off the image.
_BUFFERS = [
    # NB: decode_input's demo RECORD tail writes at DS:[DEMO_PTR + 0x3F] with the cursor running to
    # RECORD_LIMIT 0x7FC, so ANY byte in 0x003F..0x083C is writable at runtime. Every buffer below carves that
    # span; a hole between them is an un-routed write, i.e. a crash under readonly_image=True. Two holes were
    # found that way by actually playing (0x006D..0x00D5 and 0x0125..0x02E9 — 558 bytes) after the boot-flip
    # made the object graph the product default; the corpus never recorded long enough to reach them.
    # tests/test_object_graph_covers_demo_span.py now pins the whole span as routed.
    ("level_scratch_lo", 0x003F, 0xD6 - 0x3F),   # was 0x6D - 0x3F: one byte short of the first real write
    ("low_scratch_125", 0x0125, 0x02EA - 0x125),  # the second hole (see above)
    ("level_scratch_mid", 0x0535, 0x1E),
    ("scenery_trigger_scratch", 0x065E, 0xC9), ("scratch_7de6", 0x7DE6, 0x24),
    # proj_slot_scratch (0xA32E..0xA340) split around the now-named hit_flag/hit_detail/burst_*/spawned_ptr/
    # anim_ready/proj_slot_ptr/bonus_debounce fields (SpawnCursor/HitScratch): the 1 residual un-named range.
    ("proj_slot_scratch_b", 0xA333, 3),
    # camera_target_scratch (0xA3F7..0xA426) trimmed to the un-named middle (CameraScript covers both ends)
    ("camera_target_scratch", 0xA407, 0xA41F - 0xA407),
    # effect_source_camera trimmed to end right before the now-named SpawnCursor fields (0x91FC..0x9202)
    ("effect_source_camera", 0x8F1D, 0x91FC - 0x8F1D),
    ("effect_pool_5570", 0x5570, 0x5648 - 0x5570),       # the 0x5570 effect/particle pool region
    ("boot_scratch_1004", 0x1004, 0x04),
    # keyboard_demo_state split around pending_key (0x2874) and the combo_ready_*/f1_key/f2_key/fire_*/
    # key_*_latch scancode flags (0x2805/0x2810/0x2811/0x282C/0x282D/0x282F-30/0x2832), now named (Input)
    ("keyboard_demo_state_a1", 0x2804, 0x2805 - 0x2804),
    ("keyboard_demo_state_a2", 0x2806, 0x2810 - 0x2806),
    ("keyboard_demo_state_a3", 0x2812, 0x282C - 0x2812),
    ("keyboard_demo_state_a4", 0x282E, 0x282F - 0x282E),
    ("keyboard_demo_state_a5", 0x2831, 0x2832 - 0x2831),
    ("keyboard_demo_state_a6", 0x2833, 0x2874 - 0x2833),
    ("keyboard_demo_state_b", 0x2875, 0x2879 - 0x2875),
    ("demo_input_buffer", 0x00D6, 0x0125 - 0x00D6),      # the demo/idle input record buffer
    # decode_input's demo-recording write lands at a runtime DEMO_PTR-relative offset that ranges over this
    # whole low-memory scratch region (was under-sized, causing a real gap during recording-triggering demos)
    ("low_scratch_2ea", 0x02EA, 0x0535 - 0x02EA),
    ("trigger_bank_scratch", 0x0553, 0x065E - 0x0553),   # the 41CA trigger/scenery bump-alloc bank
    # the demo-recording buffer (input_decode.decode_input writes at a runtime DEMO_PTR-relative offset that
    # can land anywhere in this low-memory region, up to just before attract_mode at 0x083D) + adjacent scratch
    ("low_scratch_727", 0x0727, 0x083D - 0x0727),
    ("item_collect_state", 0x6C12, 0x6C9E - 0x6C12),     # per-item collected counts (item_total, 0x6C9E-9F,
    #                                                       now named -- Progress)
    ("prop_scratch_7e0a", 0x7E0A, 0x7E1C - 0x7E0A),
    ("prop_scratch_846b", 0x846B, 0x847E - 0x846B),
    ("bonus_cell_dedup", 0xA2A8, 0x50),   # the flood-fill dedup buffer, one byte per bonus-cell index
]
_BUFFER_BYTES = sum(ln for _, _, ln in _BUFFERS)

# sparse working buffers: the last scattered scratch bytes, woven BETWEEN routed fields (so not contiguous).
# Each groups a list of specific offsets into one named bytearray. Offsets live here in the bridge, as intended.
_SPARSE = [
    # input_scratch trimmed: in_aux (0x27E9) + idle_clock (0x27F0-1) are now named (AttractState);
    # demo_ptr (0x287A-B) is now named (Input)
    ("input_scratch", (0x27F2, 0x27F3)),
    # render_ptr_scratch trimmed: col_ring (0x2DE8-9) is now named (SceneryState); scroll_copy_src (0x2DBA-B),
    # row_ring (0x2DEA-B) and scroll_anim_ctr (0x2DF5) are now named (Camera/SceneryState)
    ("render_ptr_scratch", (0x2DBE, 0x2DBF, 0x2DF2, 0x2DF6, 0x2DF7)),
    # player_flag_scratch trimmed: page_dirty/firefly_scratch_a/b (0x6BBD/0x6BC0/0x6BC1) now named (SceneryState);
    # folded in the lone leftover from misc_scratch (0x6BFF); scroll_dir (0x6BED), cam_scroll_idle (0x6BEE) and
    # scroll_target_row (0x6BF1-2) are now named (Camera). 0x6BD6: NOT a naming gap -- it's the natural carry
    # target of the 26FA record-mutation's `inc word [0x6bd5]` (confirmed via capstone, 1030:2708) rolling
    # over the adjacent frame_blink/tick byte; nothing reads it, so it stays unnamed scratch on purpose.
    # entity_vx_scratch/entity_vy_scratch (0x6BFA-D) are now named (Motion)
    ("player_flag_scratch", (0x6BD6, 0x6BE8, 0x6BFE, 0x6BFF)),
    # camera_hud_scratch fully consumed by AttackState + HitScratch -> removed
    # misc_scratch fully consumed by AttractState/SceneryState (0x6BFF folded above) -> removed
]

#: the per-level TILE tables -> LevelTables fields (P5 slice 1b). (field, base, length). These are the level's
#: content, loaded from its *.SQZ; routing them moves them out of the undifferentiated ``_level_data`` residue
#: and onto a typed object the graph owns. Each is exactly 256 = one entry per tile id, and that extent is
#: rigorous rather than guessed: each base is the next table's base minus 0x100 AND the first mutable offset
#: after each is exactly base+0x100 -- two independent derivations agreeing, matching the measured max index 255.
#: (Deliberately NOT here: spawn_offset -- ring-cursor indexed, extent not established; sprite_left_hw -- its
#: region is INTERLEAVED, even byte constant / odd byte loader-written. Both are slice-1b follow-ups.)
_LEVEL_TABLES = [
    ("ceil_props",   0x7E5E, 256),
    ("floor_props",  0x7F5E, 256),
    ("ceil_handler", 0x805E, 256),
    ("tile_props",   0x8E1D, 256),
    ("dirty_kind",   0x4DF8, 256),
]

_ARENA_LO, _ARENA_HI, _ARENA_STRIDE_END = 0x8489, 0x8C88, 0x32   # the variable-stride 2nd-pass entity list

DGROUP_BASE = 0x1A0F << 4

# dataclass fields stored as offset-free references (pre2.game.ref.ObjectRef/RawRef) instead of raw 16-bit
# pointers. The bridge swizzles ref<->offset at the byte boundary (rb/wb + the (de)serialiser) via
# pointer_layout, so the byte image stays identical while the shipped model holds a real reference. This set is
# the ONLY coupling that marks a field as a swizzled pointer; it grows one pointer family at a time.
_REF_FIELDS = frozenset({"current_hit_object", "spawned_ptr", "cam_target_ptr", "target_a", "target_b",
                        "proj_slot_ptr"})
# fields that reference the variable-stride entity ARENA (instance-aware swizzle, not the static pool one).
_ARENA_REF_FIELDS = frozenset({"def_ptr"})
# (class, field) pairs that are CURSORS into a read-only loaded asset (AssetCursor via the static pool/asset
# swizzle). Class-scoped because the field name collides across structures (anim_ptr is a real anim cursor on
# Actor, but a non-pointer scratch byte on the effect-slot pools).
_ASSET_REF_FIELDS = frozenset({
    (Actor, "anim_ptr"),               # -> anim_script region
    (Player, "anim_ptr"),              # -> player_anim region
    (CameraScript, "script_cursor"), (CameraScript, "script_ptr"),   # -> script region
    (BossScript, "m9_ptr"), (BossScript, "script_ptr"),              # -> script region
})

PLAYER_BASE = 0x4F1C          # the player render/physics record base [asm]
_RNG_LCG = 0x2CEC             # the 4-byte LCG mixer
_ROR = 0x28C1                 # the 1-word rotate generator

# absolute-offset layouts (field, dgroup offset, width, signed) — the scattered original homes of the objects'
# fields. This mapping is the ONLY place the offsets live; the game model (pre2/game/model.py) has none.
CAMERA_LAYOUT = [
    ("col", 0x2DE4, 2, False), ("row", 0x2DE6, 2, False),
    ("fine_scroll", 0x6BC4, 1, False), ("row_factor", 0x6BF8, 2, False),
    ("scroll_anim_ctr", 0x2DF5, 1, False), ("scroll_copy_src", 0x2DBA, 2, False),
    ("cam_scroll_idle", 0x6BEE, 1, False), ("scroll_dir", 0x6BED, 1, False),
    ("scroll_target_row", 0x6BF1, 2, False), ("scroll_speed_curve_ptr", 0x78C4, 2, False),
    ("prev_cam_cell_y", 0x2DE2, 2, False),
    ("anim_remap_ptr", 0x6BC2, 2, False), ("anim_remap_thresh", 0x6BD4, 1, False),
]
PROGRESS_LAYOUT = [
    ("score_lo", 0x6C0E, 2, False), ("score_hi", 0x6C10, 2, False), ("score_mid", 0x6C0C, 2, False),
    ("lives", 0x27D8, 1, False), ("energy", 0x27D6, 1, False), ("level", 0x2D8A, 1, False),
    ("bonus_letters", 0x6CA7, 1, False), ("utensils_mask", 0x6CA8, 1, False),
    ("item_total", 0x6C9E, 2, False),
]
INPUT_LAYOUT = [
    ("up", 0x27EA, 1, False), ("down", 0x27EB, 1, False), ("left", 0x27ED, 1, False),
    ("right", 0x27EC, 1, False), ("fire", 0x27E8, 1, False), ("source", 0x2879, 1, False),
    ("pending_key", 0x2874, 1, False), ("demo_ptr", 0x287A, 2, False), ("demo_byte", 0x287C, 1, False),
    ("demo_cnt", 0x287D, 1, False), ("joystick_cfg", 0x27E4, 1, False),
    ("joystick_disable", 0x27D9, 1, False),
    ("combo_ready_a", 0x2805, 1, False), ("combo_ready_b", 0x2811, 1, False),
    ("combo_ready_c", 0x282C, 1, False), ("f1_key", 0x282F, 1, False), ("f2_key", 0x2830, 1, False),
    ("fire_alt", 0x2832, 1, False), ("fire_space", 0x2810, 1, False), ("fire_latch", 0x282D, 1, False),
    ("key_1_latch", 0x27F6, 1, False), ("key_2_latch", 0x27F7, 1, False),
]
LEVEL_STATE_LAYOUT = [
    ("flags", 0x8166, 1, False), ("end_mode", 0x6BE6, 1, False), ("respawn_state", 0x6BE4, 1, False),
    ("end_signal", 0x6BE5, 1, False), ("checkpoint_x", 0x6BAD, 2, False), ("checkpoint_y", 0x6BAF, 2, False),
    ("grid_dirty", 0x2DF4, 1, False), ("level_prop_header", 0x815E, 2, False),
]
PLAYER_STATE_LAYOUT = [
    ("trail_ring", 0x6BBE, 2, False), ("glider", 0x6BC5, 1, False), ("fly_hold", 0x6BC6, 1, False),
    ("last_land_y", 0x6BCA, 2, False), ("aura_toggle", 0x6BCC, 1, False),
    ("input_suppress", 0x6BCD, 1, False), ("anim_hi", 0x6BCF, 1, False),
    ("frame_blink", 0x6BD5, 1, False), ("input_lr", 0x6BDB, 1, False), ("input_ud", 0x6BDC, 1, False),
    ("drop_gate", 0x6BE1, 1, False), ("scale_level", 0x6BE2, 2, False), ("camera_shake", 0x6BEA, 1, False),
    ("run_count", 0x6BEB, 2, False), ("friction", 0x6BF6, 2, False),
]
SCROLL_LAYOUT = [
    ("bonus_flash", 0x6C00, 1, False), ("to_dark", 0x6C01, 1, False), ("to_light", 0x6C02, 1, False),
    ("step", 0x6C03, 1, False), ("lights_off", 0x6C04, 1, False), ("phase", 0x6C05, 1, False),
    ("vx", 0x6C06, 2, False), ("vy", 0x6C08, 2, False), ("script_last", 0x6C0A, 2, False),
    ("scroll_push", 0x91FB, 1, False),
]
MOTION_LAYOUT = [
    ("airborne", 0x6BF3, 1, False), ("fall_frames", 0x6BD2, 1, False), ("fall_latch", 0x6BD1, 1, False),
    ("fall_grace", 0x6BE0, 1, False), ("low_gravity", 0x6BC7, 1, False), ("fly_timer", 0x6BC8, 1, False),
    ("idle_timer", 0x6BD3, 1, False), ("anim_gate", 0x6BD0, 1, False), ("charge", 0x6BCE, 1, False),
    ("hurt_cooldown", 0x6BC9, 1, False),
    ("entity_vx_scratch", 0x6BFA, 2, False), ("entity_vy_scratch", 0x6BFC, 2, False),
]
ATTACK_STATE_LAYOUT = [("attack_phase", 0x7B18, 1, False), ("attack_v19", 0x7B19, 1, False),
                       ("glider_tilt", 0x7B1A, 1, False)]
HIT_SCRATCH_LAYOUT = [("quake_dist_lo", 0xA30E, 2, False), ("quake_dist_hi", 0xA310, 2, False),
                      ("hit_pass_full", 0xA312, 1, False), ("hit_flag", 0xA330, 1, False),
                      ("hit_detail", 0xA331, 2, False), ("bonus_debounce", 0xA33C, 2, False)]
SPAWN_CURSOR_LAYOUT = [
    ("spawn_count", 0x91FC, 2, False), ("cam_state", 0x91FE, 1, False), ("cursor_x", 0x91FF, 2, False),
    ("cursor_y", 0x9201, 2, False), ("burst_x", 0xA336, 2, False), ("burst_y", 0xA338, 2, False),
    ("burst_sprite", 0xA33A, 2, False), ("spawned_ptr", 0xA33E, 2, False), ("anim_ready", 0xA340, 1, False),
    ("spawn_offset_ring", 0xA341, 2, False), ("proj_slot_ptr", 0xA32E, 2, False),
    ("cursor_x_lo", 0x91F7, 2, False), ("cursor_x_hi", 0x91F9, 2, False),
]
CAMERA_SCRIPT_LAYOUT = [
    ("cam_timer", 0xA3F7, 2, False), ("cmd_byte", 0xA3F9, 1, False), ("dist_dir", 0xA3FA, 1, False),
    ("dist_x", 0xA3FB, 2, False), ("hit_debounce", 0xA3FD, 2, False), ("script_cursor", 0xA3FF, 2, False),
    ("script_ptr", 0xA401, 2, False), ("cursor_latch_x", 0xA403, 2, False), ("cursor_latch_y", 0xA405, 2, False),
    ("cam_param_e", 0xA41F, 2, False), ("cam_target_ptr", 0xA421, 2, False), ("target_a", 0xA423, 2, False),
    ("target_b", 0xA425, 2, False),
]
SCENERY_STATE_LAYOUT = [
    ("map_rows", 0x2CF5, 1, False), ("map_seg", 0x2DDA, 2, False), ("dipping_tile", 0x6BAB, 2, False),
    ("current_hit_object", 0x6BB1, 2, False),
    ("page_dirty", 0x6BBD, 1, False), ("grid_dirty_token", 0x2DE0, 2, False), ("col_ring", 0x2DE8, 2, False),
    ("row_ring", 0x2DEA, 2, False),
    ("sprite_bank_lo", 0x8C89, 2, False), ("sprite_bank_hi", 0x8C8B, 2, False),
    ("firefly_scratch_a", 0x6BC0, 1, False), ("firefly_scratch_b", 0x6BC1, 1, False),
    ("collected_linked", 0x2A7A, 2, False), ("display_page", 0x2DD6, 2, False), ("cam_left", 0x8164, 2, False),
    ("collected_counter", 0x2A76, 2, False),
]
ATTRACT_STATE_LAYOUT = [("attract_mode", 0x083D, 1, False), ("attract_level", 0x083E, 1, False),
                        ("in_aux", 0x27E9, 1, False), ("idle_clock", 0x27F0, 2, False)]
DIFFICULTY_MODE_LAYOUT = [("mode", 0xB197, 1, False), ("mode_copy", 0xB198, 1, False)]
BOSS_LAYOUT = [
    ("boss_phase", 0xA326, 2, False),
    ("l6_stun", 0xA324, 1, False), ("l6_hits", 0xA325, 1, False), ("l6_anim", 0xA328, 1, False),
    ("l6_sub_b", 0xA329, 1, False), ("l6_sub_a", 0xA32A, 1, False), ("l6_timer", 0xA32B, 1, False),
    ("l6_reseed", 0xA32C, 1, False),
]
BOSS_SCRIPT_LAYOUT = [
    ("script_ptr", 0xA517, 2, False), ("dwell", 0xA515, 1, False), ("cycle", 0xA516, 1, False),
    ("m9_ptr", 0xA4F7, 2, False), ("m9_count", 0xA519, 2, False),
]
# the 12-slot object/enemy list at 0x4FD0 (stride 0x12); the offsets are relative to each slot
ACTOR_LAYOUT = [
    ("x", 0x00, 2, False), ("y", 0x02, 2, False), ("sprite", 0x04, 2, False), ("def_ptr", 0x06, 2, False),
    ("xvel", 0x08, 2, False), ("yvel", 0x0A, 2, False), ("anim_ptr", 0x0C, 2, False),
    ("state", 0x0E, 1, False), ("hp", 0x0F, 1, False), ("hits", 0x10, 1, False), ("life", 0x11, 1, False),
]
ACTOR_BASE, ACTOR_COUNT, ACTOR_STRIDE = 0x4FD0, 12, 0x12
# projectile / burst / debris sprite lists (all stride 0x12, RenderSlot + velocity layout)
EFFECT_SLOT_LAYOUT = [
    ("x", 0x00, 2, False), ("y", 0x02, 2, False), ("sprite", 0x04, 2, False), ("xvel", 0x06, 2, False),
    ("kind", 0x08, 1, False), ("source", 0x09, 2, False), ("aux_b", 0x0B, 1, False),
    ("anim_ptr", 0x0C, 2, False), ("yvel", 0x0E, 2, False), ("aux_10", 0x10, 1, False), ("life", 0x11, 1, False),
]
_PROJECTILE_BASE, _PROJECTILE_COUNT = 0x4F2E, 4     # the player's thrown-weapon projectiles
_RING_BASE, _RING_COUNT = 0x4F76, 5                 # the sparkle / popup trail ring (0x4F76..0x4FD0)
_BURST_BASE, _BURST_COUNT = 0x50A8, 32              # the score/effect-burst free object pool (0x50A8..0x52E8);
                                                     # (0x52E8-0x50A8)/0x12 = 32 exactly (was wrongly 20)
_DST_BASE, _DST_COUNT = 0x52E8, 20                  # the DST effect-particle pool (0x52E8..0x5450)
_DEBRIS_BASE, _DEBRIS_COUNT = 0x5450, 16            # the debris-element pool
_SLOT0_BASE, _SLOT0_COUNT = 0x4F0A, 1               # render slot 0 (the one below the player)
_EFFECT_ROW_BASE, _EFFECT_ROW_COUNT = 0x56A2, 8     # the horizontal effect-row sprites
_TARGET_BASE, _TARGET_COUNT = 0x5648, 5             # the 5 camera-target flash records
# the wall-impact marker table (0x6EA9..0x6F49, 20 x 8-byte records = four words each)
WALL_MARKER_LAYOUT = [("token", 0, 2, False), ("map_off", 2, 2, False), ("data0", 4, 2, False),
                      ("data1", 6, 2, False)]
_WALL_MARKER_BASE, _WALL_MARKER_COUNT = 0x6EA9, 20
# the 80-cell bonus/collectible list (0x8C8D..0x8E1D, stride 5)
BONUS_CELL_LAYOUT = [("tile_num0", 0, 1, False), ("tile_num1", 1, 1, False), ("count", 2, 1, False),
                     ("cell", 3, 2, False)]
_BONUS_CELL_BASE, _BONUS_CELL_COUNT = 0x8C8D, 0x50

# (field, offset, width, signed). Player offsets are relative to PLAYER_BASE; Rng offsets are absolute DGROUP.
PLAYER_LAYOUT = [
    ("x", 0x00, 2, False), ("y", 0x02, 2, False), ("sprite", 0x04, 2, False),
    ("xvel", 0x06, 2, True), ("motion_mode", 0x08, 1, False), ("facing", 0x09, 2, True),
    ("anim_b", 0x0B, 1, False), ("anim_ptr", 0x0C, 2, False), ("yvel", 0x0E, 2, True),
    ("run_flag", 0x10, 1, False), ("death_state", 0x11, 1, False),
]
RNG_LAYOUT = [
    ("lcg_a", _RNG_LCG + 0, 1, False), ("lcg_b", _RNG_LCG + 1, 1, False),
    ("lcg_c", _RNG_LCG + 2, 1, False), ("lcg_d", _RNG_LCG + 3, 2, False),
    ("ror", _ROR, 2, False),
]


def _rd(data, base, off, width, signed):
    b = DGROUP_BASE + base + off
    v = data[b] if width == 1 else data[b] | (data[b + 1] << 8)
    if signed and v & (1 << (8 * width - 1)):
        v -= 1 << (8 * width)
    return v


def _wr(data, base, off, width, v):
    b = DGROUP_BASE + base + off
    v &= (1 << (8 * width)) - 1
    data[b] = v & 0xFF
    if width == 2:
        data[b + 1] = (v >> 8) & 0xFF


def _obj_from_image(cls, layout, data, base=0):
    data = getattr(data, "data", data)
    vals = {}
    for f, off, w, s in layout:
        v = _rd(data, base, off, w, s)
        if f in _REF_FIELDS:                     # swizzle the raw offset -> an offset-free reference
            from pre2.native.pointer_layout import from_offset
            v = from_offset(v & 0xFFFF)
        vals[f] = v
    return cls(**vals)


def _obj_to_image(obj, layout, data, base=0, arena_to_offset=None):
    data = getattr(data, "data", data)
    for f, off, w, _s in layout:
        v = getattr(obj, f)
        if f in _REF_FIELDS or (type(obj), f) in _ASSET_REF_FIELDS:   # pool/asset ref -> offset (int-tolerant)
            from pre2.native.pointer_layout import to_offset
            v = to_offset(v)
        elif f in _ARENA_REF_FIELDS and arena_to_offset is not None:   # arena ref -> offset (instance-aware)
            v = arena_to_offset(v)
        _wr(data, base, off, w, v)


# the structures routed to live dataclasses: (attribute, class, layout, base, count, stride). count>1 = a LIST
# of `count` objects at base + k*stride. Everything else stays in the image. Grows toward the endpoint (whole
# tick on objects) one structure at a time.
_ROUTES = [
    ("player", Player, PLAYER_LAYOUT, PLAYER_BASE, 1, 0),
    ("rng", Rng, RNG_LAYOUT, 0, 1, 0),
    ("camera", Camera, CAMERA_LAYOUT, 0, 1, 0),
    ("progress", Progress, PROGRESS_LAYOUT, 0, 1, 0),
    ("input", Input, INPUT_LAYOUT, 0, 1, 0),
    ("level_state", LevelState, LEVEL_STATE_LAYOUT, 0, 1, 0),
    ("motion", Motion, MOTION_LAYOUT, 0, 1, 0),
    ("player_state", PlayerState, PLAYER_STATE_LAYOUT, 0, 1, 0),
    ("scroll", Scroll, SCROLL_LAYOUT, 0, 1, 0),
    ("attack_state", AttackState, ATTACK_STATE_LAYOUT, 0, 1, 0),
    ("hit_scratch", HitScratch, HIT_SCRATCH_LAYOUT, 0, 1, 0),
    ("spawn_cursor", SpawnCursor, SPAWN_CURSOR_LAYOUT, 0, 1, 0),
    ("camera_script", CameraScript, CAMERA_SCRIPT_LAYOUT, 0, 1, 0),
    ("scenery_state", SceneryState, SCENERY_STATE_LAYOUT, 0, 1, 0),
    ("attract_state", AttractState, ATTRACT_STATE_LAYOUT, 0, 1, 0),
    ("difficulty_mode", DifficultyMode, DIFFICULTY_MODE_LAYOUT, 0, 1, 0),
    ("boss", Boss, BOSS_LAYOUT, 0, 1, 0),
    ("boss_script", BossScript, BOSS_SCRIPT_LAYOUT, 0, 1, 0),
    ("actors", Actor, ACTOR_LAYOUT, ACTOR_BASE, ACTOR_COUNT, ACTOR_STRIDE),
    ("projectiles", EffectSlot, EFFECT_SLOT_LAYOUT, _PROJECTILE_BASE, _PROJECTILE_COUNT, 0x12),
    ("popup_ring", EffectSlot, EFFECT_SLOT_LAYOUT, _RING_BASE, _RING_COUNT, 0x12),
    ("bursts", EffectSlot, EFFECT_SLOT_LAYOUT, _BURST_BASE, _BURST_COUNT, 0x12),
    ("dst_pool", EffectSlot, EFFECT_SLOT_LAYOUT, _DST_BASE, _DST_COUNT, 0x12),
    ("debris", EffectSlot, EFFECT_SLOT_LAYOUT, _DEBRIS_BASE, _DEBRIS_COUNT, 0x12),
    ("effect_row", EffectSlot, EFFECT_SLOT_LAYOUT, _EFFECT_ROW_BASE, _EFFECT_ROW_COUNT, 0x12),
    ("target_records", EffectSlot, EFFECT_SLOT_LAYOUT, _TARGET_BASE, _TARGET_COUNT, 0x12),
    ("slot0", EffectSlot, EFFECT_SLOT_LAYOUT, _SLOT0_BASE, _SLOT0_COUNT, 0x12),
    ("wall_markers", WallMarker, WALL_MARKER_LAYOUT, _WALL_MARKER_BASE, _WALL_MARKER_COUNT, 8),
    ("bonus_cells", BonusCell, BONUS_CELL_LAYOUT, _BONUS_CELL_BASE, _BONUS_CELL_COUNT, 5),
]


def _arena_to_offset(arena, ref) -> int:
    """``ArenaRef``/``RawRef``/a bare int -> the position ``arena`` (a ``list[(pos, ArenaEntity)]``) recorded for
    that entity. Tolerates a bare int (a not-yet-swizzled field) so the serialiser works either way."""
    from pre2.game.ref import ArenaRef, RawRef
    if isinstance(ref, int):
        return ref & 0xFFFF
    if isinstance(ref, RawRef):
        return ref.value & 0xFFFF
    if isinstance(ref, ArenaRef):
        return arena[ref.index][0] & 0xFFFF
    raise TypeError(f"not an arena reference: {ref!r}")


def _arena_from_offset(arena, v: int):
    """Instance-aware swizzle for a pointer INTO the variable-stride entity arena: a position that matches one of
    THIS state's parsed record starts -> an ``ArenaRef(index)``; anything else -> an opaque ``RawRef``."""
    from pre2.game.ref import ArenaRef, RawRef
    v &= 0xFFFF
    for idx, (start, _e) in enumerate(arena):
        if start == v:
            return ArenaRef(idx)
    return RawRef(v)


class _RefSwizzle:
    """The offset-knowledge this bridge injects into the shipped :class:`ObjectGraphStore` so its rb/wb compat
    shim can still serve legacy (not-yet-converted) callers that touch a reference-typed field. Resolves all
    three flavors: static pool (``ObjectRef``, via ``pointer_layout``), the variable-stride arena (``ArenaRef``,
    instance-aware against THIS state's parse), and loaded assets (``AssetCursor``, also ``pointer_layout``)."""

    __slots__ = ("_arena",)

    def __init__(self, arena):
        self._arena = arena

    def is_ref_field(self, inst, field) -> bool:
        return field in _REF_FIELDS or field in _ARENA_REF_FIELDS or (type(inst), field) in _ASSET_REF_FIELDS

    def to_key(self, ref) -> int:
        from pre2.game.ref import ArenaRef
        if isinstance(ref, ArenaRef):
            return _arena_to_offset(self._arena, ref)
        from pre2.native.pointer_layout import to_offset
        return to_offset(ref)

    def from_key(self, inst, field, key: int):
        if field in _ARENA_REF_FIELDS:
            return _arena_from_offset(self._arena, key)
        from pre2.native.pointer_layout import from_offset
        return from_offset(key)


class DataclassBackend(ObjectGraphStore):
    """Run the game with named structures' live state as real offset-free dataclasses, not bytes.

    The north-star mechanism: ``NativeGameState.backend`` swaps to this and the gameplay tick runs UNCHANGED,
    but every read/write that lands in a routed structure's byte range is redirected — via the bridge layout —
    to/from the fields of a live dataclass (``self.player.x``, ``self.camera.col``, ``self.progress.lives``,
    ...). Everything else stays in the image. The offsets live ONLY here in the bridge mapping, not in the game
    logic or the store: this class does the offset-AWARE construction (parsing an image into named objects +
    the byte-compat map) and (de)serialisation; the actual object/map storage and rb/rw compat shim it hands
    off to are the SHIPPED, offset-free :class:`~pre2.native.object_state.ObjectGraphStore`. Each structure
    added to ``_ROUTES`` shrinks the image's live surface. ``materialize`` folds the objects back to bytes for
    the digest.
    """

    __slots__ = ("_img", "_arena", "_sparse")

    def __init__(self, seed, readonly_image=False):
        # readonly_image=True asserts the invariant that the gameplay tick writes NOTHING to the loaded data (all
        # mutable state is on the object graph); any fallback write then raises. ``level_data`` is the tick's own
        # copy of the read-only loaded tables — so (objects + level_data) is a SELF-CONTAINED state of record and
        # the tick needs no external byte image at all (``_img`` is only a materialize target for the render).
        data = getattr(seed, "data", seed)
        self._img = data
        objs: dict[str, object] = {}
        # absolute dgroup offset -> (object, field, byte_index, width, signed) — mapped straight to the instance
        map_: dict[int, tuple] = {}
        for attr, cls, layout, base, count, stride in _ROUTES:
            insts = [_obj_from_image(cls, layout, data, base + k * stride) for k in range(count)]
            objs[attr] = insts[0] if count == 1 else insts
            for k, inst in enumerate(insts):
                b0 = base + k * stride
                for f, off, w, s in layout:
                    for bk in range(w):
                        map_[(b0 + off + bk) & 0xFFFF] = (inst, f, bk, w, s)
        # the variable-stride entity arena (0x8489): parse once (boundaries stable during a tick) and route each
        # record's header fields + body bytes. A body byte is marked with width 0 (index into ``inst.body``).
        self._arena: list[tuple[int, ArenaEntity]] = []
        i = _ARENA_LO
        while i <= _ARENA_HI:
            b = DGROUP_BASE + i
            st = data[b]
            if st == 0 or st >= _ARENA_STRIDE_END:
                break
            e = ArenaEntity(st, data[b + 1], data[b + 2] | (data[b + 3] << 8), data[b + 4],
                            bytearray(data[b + 5:b + st]))
            self._arena.append((i, e))
            hdr = [("stride", 0, 1), ("flags1", 1, 1), ("sprite_ref", 2, 2), ("skip", 4, 1)]
            for f, off, w in hdr:
                for bk in range(w):
                    map_[(i + off + bk) & 0xFFFF] = (e, f, bk, w, False)
            for k in range(st - 5):
                map_[(i + 5 + k) & 0xFFFF] = (e, "body", k, 0, False)
            i += st
        objs["entities"] = [e for _, e in self._arena]
        # POST-PASS: now that the arena is parsed, swizzle every _ARENA_REF_FIELDS pointer (def_ptr) parsed as a
        # raw int by _obj_from_image into an ArenaRef against the just-built arena — a real reference to the source
        # entity. (Done here, not in _obj_from_image, because the arena is parsed AFTER the pooled structures.)
        for attr, cls, layout, base, count, stride in _ROUTES:
            arena_fs = [f for f, *_ in layout if f in _ARENA_REF_FIELDS]
            asset_fs = [f for f, *_ in layout if (cls, f) in _ASSET_REF_FIELDS]
            if not arena_fs and not asset_fs:
                continue
            insts = objs[attr] if count > 1 else [objs[attr]]
            for inst in insts:
                for f in arena_fs:                   # arena ref: instance-aware
                    setattr(inst, f, _arena_from_offset(self._arena, getattr(inst, f)))
                for f in asset_fs:                   # asset cursor: static pool/asset swizzle
                    from pre2.native.pointer_layout import from_offset
                    setattr(inst, f, from_offset(getattr(inst, f)))
        # the per-level TILE tables -> one typed LevelTables (P5 slice 1b). Same width-0 byte mapping the
        # buffers use, but onto NAMED fields: these are the level's content, not scratch. They leave the
        # undifferentiated _level_data residue and become part of the object graph proper.
        lt = LevelTables(**{f: bytearray(data[DGROUP_BASE + base:DGROUP_BASE + base + ln])
                            for f, base, ln in _LEVEL_TABLES})
        objs["level_tables"] = lt
        for f, base, ln in _LEVEL_TABLES:
            for k in range(ln):
                map_[(base + k) & 0xFFFF] = (lt, f, k, 0, False)
        # named working-memory buffers (raw bytearrays), mapped byte-for-byte (width 0 -> index into .data)
        for attr, base, ln in _BUFFERS:
            buf = ByteBuffer(attr, bytearray(data[DGROUP_BASE + base:DGROUP_BASE + base + ln]))
            objs[attr] = buf
            for k in range(ln):
                map_[(base + k) & 0xFFFF] = (buf, "data", k, 0, False)
        # sparse working buffers (scattered offsets -> one bytearray, indexed by position)
        self._sparse = []
        for attr, offs in _SPARSE:
            offs = tuple(sorted(offs))
            buf = ByteBuffer(attr, bytearray(data[DGROUP_BASE + o] for o in offs))
            objs[attr] = buf
            self._sparse.append((offs, buf))
            for i, o in enumerate(offs):
                map_[o & 0xFFFF] = (buf, "data", i, 0, False)
        level_data = bytearray(data[DGROUP_BASE:DGROUP_BASE + 0x10000])
        super().__init__(objs, map_, level_data, readonly_image, ref_swizzle=_RefSwizzle(self._arena))

    def arena_from_offset(self, v: int):
        """See :func:`_arena_from_offset` — kept as a public method for existing bridge-side callers."""
        return _arena_from_offset(self._arena, v)

    def arena_to_offset(self, ref) -> int:
        """See :func:`_arena_to_offset` — kept as a public method for existing bridge-side callers."""
        return _arena_to_offset(self._arena, ref)

    def materialize(self, data=None) -> None:
        # Self-contained: reconstruct the whole DGROUP from (loaded tables + the object graph), no external state.
        data = self._img if data is None else data
        data[DGROUP_BASE:DGROUP_BASE + 0x10000] = self._level_data
        for attr, _cls, layout, base, count, stride in _ROUTES:
            insts = self._objs[attr] if count > 1 else [self._objs[attr]]
            for k, inst in enumerate(insts):
                _obj_to_image(inst, layout, data, base + k * stride, arena_to_offset=self.arena_to_offset)
        for start, e in self._arena:
            b = DGROUP_BASE + start
            data[b] = e.stride & 0xFF
            data[b + 1] = e.flags1 & 0xFF
            data[b + 2] = e.sprite_ref & 0xFF
            data[b + 3] = (e.sprite_ref >> 8) & 0xFF
            data[b + 4] = e.skip & 0xFF
            data[b + 5:b + 5 + len(e.body)] = e.body
        lt = self._objs["level_tables"]                       # the typed per-level tile tables (slice 1b)
        for f, base, ln in _LEVEL_TABLES:
            data[DGROUP_BASE + base:DGROUP_BASE + base + ln] = getattr(lt, f)
        for attr, base, ln in _BUFFERS:
            buf = self._objs[attr]
            data[DGROUP_BASE + base:DGROUP_BASE + base + ln] = buf.data
        for offs, buf in self._sparse:
            for i, o in enumerate(offs):
                data[DGROUP_BASE + o] = buf.data[i]
