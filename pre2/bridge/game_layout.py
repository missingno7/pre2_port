"""The DETACHABLE bridge: the ONLY place that knows the original DOS byte layout of the game model.

``pre2/game/model.py`` is the clean, offset-free game (the shipped product). This module is the umbilical cord:
it maps those dataclasses to/from the original DGROUP byte image so the model can be verified byte-for-byte
against the DOS original. Ship without this module and the game has no notion of offsets, no byte image, and
therefore no replay/snapshot — it is just the object model.

A layout is ``(field, rel_offset, width, signed)`` per canonical field; the alias bytes (``flags``,
``facing_lo``, ``life``) are re-projections of a canonical field's bytes, so writing the canonical fields
reproduces them exactly — no separate entries needed. Evidence for each offset lives with the ``dgroup_view``
descriptors (the recovery spec); this table is the machine-readable serialisation layout.
"""
from __future__ import annotations

from pre2.game.model import (Actor, ArenaEntity, AttackState, AttractState, Boss, BonusCell, BossScript,
                             ByteBuffer, Camera, CameraScript, DifficultyMode, EffectSlot, HitScratch, Input,
                             LevelState, Motion, Player, PlayerState, Progress, Rng, SceneryState, Scroll,
                             SpawnCursor, WallMarker)

# named working-memory buffers the tick scribbles as raw bytes: (attr, base, length). Modeled as a bytearray,
# not fields (transient scratch, not records). These finish draining the mutable state off the image.
_BUFFERS = [
    ("level_scratch_lo", 0x003F, 0x6D - 0x3F), ("level_scratch_mid", 0x0535, 0x1E),
    ("scenery_trigger_scratch", 0x065E, 0xC9), ("scratch_7de6", 0x7DE6, 0x24),
    # proj_slot_scratch (0xA32E..0xA340) split around the now-named hit_flag/hit_detail/burst_*/spawned_ptr/
    # anim_ready fields (SpawnCursor/HitScratch): the 3 residual un-named ranges.
    ("proj_slot_scratch_a", 0xA32E, 2), ("proj_slot_scratch_b", 0xA333, 3), ("proj_slot_scratch_c", 0xA33C, 2),
    # camera_target_scratch (0xA3F7..0xA426) trimmed to the un-named middle (CameraScript covers both ends)
    ("camera_target_scratch", 0xA407, 0xA41F - 0xA407),
    # effect_source_camera trimmed to end right before the now-named SpawnCursor fields (0x91FC..0x9202)
    ("effect_source_camera", 0x8F1D, 0x91FC - 0x8F1D),
    ("effect_pool_5570", 0x5570, 0x5648 - 0x5570),       # the 0x5570 effect/particle pool region
    ("boot_scratch_1004", 0x1004, 0x04),
    ("keyboard_demo_state", 0x2804, 0x2879 - 0x2804),    # the keyboard scan + demo-cursor state
    ("demo_input_buffer", 0x00D6, 0x0125 - 0x00D6),      # the demo/idle input record buffer
    # decode_input's demo-recording write lands at a runtime DEMO_PTR-relative offset that ranges over this
    # whole low-memory scratch region (was under-sized, causing a real gap during recording-triggering demos)
    ("low_scratch_2ea", 0x02EA, 0x0535 - 0x02EA),
    ("trigger_bank_scratch", 0x0553, 0x065E - 0x0553),   # the 41CA trigger/scenery bump-alloc bank
    # the demo-recording buffer (input_decode.decode_input writes at a runtime DEMO_PTR-relative offset that
    # can land anywhere in this low-memory region, up to just before attract_mode at 0x083D) + adjacent scratch
    ("low_scratch_727", 0x0727, 0x083D - 0x0727),
    ("item_collect_state", 0x6C12, 0x6CA0 - 0x6C12),     # per-item collected counts + totals
    ("prop_scratch_7e0a", 0x7E0A, 0x7E1C - 0x7E0A),
    ("prop_scratch_846b", 0x846B, 0x847E - 0x846B),
    ("bonus_cell_dedup", 0xA2A8, 0x50),   # the flood-fill dedup buffer, one byte per bonus-cell index
]
_BUFFER_BYTES = sum(ln for _, _, ln in _BUFFERS)

# sparse working buffers: the last scattered scratch bytes, woven BETWEEN routed fields (so not contiguous).
# Each groups a list of specific offsets into one named bytearray. Offsets live here in the bridge, as intended.
_SPARSE = [
    # input_scratch trimmed: in_aux (0x27E9) + idle_clock (0x27F0-1) are now named (AttractState)
    ("input_scratch", (0x27F2, 0x27F3, 0x287A, 0x287B)),
    # render_ptr_scratch trimmed: col_ring (0x2DE8-9) is now named (SceneryState)
    ("render_ptr_scratch", (0x2DBA, 0x2DBB, 0x2DBE, 0x2DBF, 0x2DEA, 0x2DEB, 0x2DF2, 0x2DF5, 0x2DF6, 0x2DF7)),
    # player_flag_scratch trimmed: page_dirty/firefly_scratch_a/b (0x6BBD/0x6BC0/0x6BC1) now named (SceneryState);
    # folded in the lone leftover from misc_scratch (0x6BFF)
    ("player_flag_scratch", (0x6BD6, 0x6BE8, 0x6BED, 0x6BEE, 0x6BF1, 0x6BF2, 0x6BFA, 0x6BFB, 0x6BFC, 0x6BFD,
                             0x6BFE, 0x6BFF)),
    # camera_hud_scratch fully consumed by AttackState + HitScratch -> removed
    # misc_scratch fully consumed by AttractState/SceneryState (0x6BFF folded above) -> removed
]

_ARENA_LO, _ARENA_HI, _ARENA_STRIDE_END = 0x8489, 0x8C88, 0x32   # the variable-stride 2nd-pass entity list

DGROUP_BASE = 0x1A0F << 4

# dataclass fields stored as offset-free references (pre2.game.ref.ObjectRef/RawRef) instead of raw 16-bit
# pointers. The bridge swizzles ref<->offset at the byte boundary (rb/wb + the (de)serialiser) via
# pointer_layout, so the byte image stays identical while the shipped model holds a real reference. This set is
# the ONLY coupling that marks a field as a swizzled pointer; it grows one pointer family at a time.
_REF_FIELDS = frozenset({"current_hit_object"})

PLAYER_BASE = 0x4F1C          # the player render/physics record base [asm]
_RNG_LCG = 0x2CEC             # the 4-byte LCG mixer
_ROR = 0x28C1                 # the 1-word rotate generator

# absolute-offset layouts (field, dgroup offset, width, signed) — the scattered original homes of the objects'
# fields. This mapping is the ONLY place the offsets live; the game model (pre2/game/model.py) has none.
CAMERA_LAYOUT = [
    ("col", 0x2DE4, 2, False), ("row", 0x2DE6, 2, False),
    ("fine_scroll", 0x6BC4, 1, False), ("row_factor", 0x6BF8, 2, False),
]
PROGRESS_LAYOUT = [
    ("score_lo", 0x6C0E, 2, False), ("score_hi", 0x6C10, 2, False),
    ("lives", 0x27D8, 1, False), ("energy", 0x27D6, 1, False), ("level", 0x2D8A, 1, False),
    ("bonus_letters", 0x6CA7, 1, False), ("utensils_mask", 0x6CA8, 1, False),
]
INPUT_LAYOUT = [
    ("up", 0x27EA, 1, False), ("down", 0x27EB, 1, False), ("left", 0x27ED, 1, False),
    ("right", 0x27EC, 1, False), ("fire", 0x27E8, 1, False), ("source", 0x2879, 1, False),
]
LEVEL_STATE_LAYOUT = [
    ("flags", 0x8166, 1, False), ("end_mode", 0x6BE6, 1, False), ("respawn_state", 0x6BE4, 1, False),
    ("end_signal", 0x6BE5, 1, False), ("checkpoint_x", 0x6BAD, 2, False), ("checkpoint_y", 0x6BAF, 2, False),
    ("grid_dirty", 0x2DF4, 1, False),
]
PLAYER_STATE_LAYOUT = [
    ("trail_ring", 0x6BBE, 2, False), ("glider", 0x6BC5, 1, False), ("fly_hold", 0x6BC6, 1, False),
    ("last_land_y", 0x6BCA, 2, False), ("input_suppress", 0x6BCD, 1, False), ("anim_hi", 0x6BCF, 1, False),
    ("frame_blink", 0x6BD5, 1, False), ("input_lr", 0x6BDB, 1, False), ("input_ud", 0x6BDC, 1, False),
    ("drop_gate", 0x6BE1, 1, False), ("scale_level", 0x6BE2, 2, False), ("camera_shake", 0x6BEA, 1, False),
    ("run_count", 0x6BEB, 2, False), ("friction", 0x6BF6, 2, False),
]
SCROLL_LAYOUT = [
    ("bonus_flash", 0x6C00, 1, False), ("to_dark", 0x6C01, 1, False), ("to_light", 0x6C02, 1, False),
    ("step", 0x6C03, 1, False), ("lights_off", 0x6C04, 1, False), ("phase", 0x6C05, 1, False),
    ("vx", 0x6C06, 2, False), ("vy", 0x6C08, 2, False), ("script_last", 0x6C0A, 2, False),
]
MOTION_LAYOUT = [
    ("airborne", 0x6BF3, 1, False), ("fall_frames", 0x6BD2, 1, False), ("fall_latch", 0x6BD1, 1, False),
    ("fall_grace", 0x6BE0, 1, False), ("low_gravity", 0x6BC7, 1, False), ("fly_timer", 0x6BC8, 1, False),
    ("idle_timer", 0x6BD3, 1, False), ("anim_gate", 0x6BD0, 1, False), ("charge", 0x6BCE, 1, False),
    ("hurt_cooldown", 0x6BC9, 1, False),
]
ATTACK_STATE_LAYOUT = [("attack_phase", 0x7B18, 1, False), ("attack_v19", 0x7B19, 1, False),
                       ("glider_tilt", 0x7B1A, 1, False)]
HIT_SCRATCH_LAYOUT = [("quake_dist_lo", 0xA30E, 2, False), ("quake_dist_hi", 0xA310, 2, False),
                      ("hit_pass_full", 0xA312, 1, False), ("hit_flag", 0xA330, 1, False),
                      ("hit_detail", 0xA331, 2, False)]
SPAWN_CURSOR_LAYOUT = [
    ("spawn_count", 0x91FC, 2, False), ("cam_state", 0x91FE, 1, False), ("cursor_x", 0x91FF, 2, False),
    ("cursor_y", 0x9201, 2, False), ("burst_x", 0xA336, 2, False), ("burst_y", 0xA338, 2, False),
    ("burst_sprite", 0xA33A, 2, False), ("spawned_ptr", 0xA33E, 2, False), ("anim_ready", 0xA340, 1, False),
    ("spawn_offset_ring", 0xA341, 2, False),
]
CAMERA_SCRIPT_LAYOUT = [
    ("cam_timer", 0xA3F7, 2, False), ("cmd_byte", 0xA3F9, 1, False), ("dist_dir", 0xA3FA, 1, False),
    ("dist_x", 0xA3FB, 2, False), ("hit_debounce", 0xA3FD, 2, False), ("script_cursor", 0xA3FF, 2, False),
    ("script_ptr", 0xA401, 2, False), ("cursor_latch_x", 0xA403, 2, False), ("cursor_latch_y", 0xA405, 2, False),
    ("cam_param_e", 0xA41F, 2, False), ("cam_target_ptr", 0xA421, 2, False), ("target_a", 0xA423, 2, False),
    ("target_b", 0xA425, 2, False),
]
SCENERY_STATE_LAYOUT = [
    ("map_rows", 0x2CF5, 1, False), ("dipping_tile", 0x6BAB, 2, False),
    ("current_hit_object", 0x6BB1, 2, False),
    ("page_dirty", 0x6BBD, 1, False), ("grid_dirty_token", 0x2DE0, 2, False), ("col_ring", 0x2DE8, 2, False),
    ("sprite_bank_lo", 0x8C89, 2, False), ("sprite_bank_hi", 0x8C8B, 2, False),
    ("firefly_scratch_a", 0x6BC0, 1, False), ("firefly_scratch_b", 0x6BC1, 1, False),
    ("collected_linked", 0x2A7A, 2, False), ("display_page", 0x2DD6, 2, False), ("cam_left", 0x8164, 2, False),
    ("collected_counter", 0x2A76, 2, False),
]
ATTRACT_STATE_LAYOUT = [("attract_mode", 0x083D, 1, False), ("attract_level", 0x083E, 1, False),
                        ("in_aux", 0x27E9, 1, False), ("idle_clock", 0x27F0, 2, False)]
DIFFICULTY_MODE_LAYOUT = [("mode", 0xB197, 1, False), ("mode_copy", 0xB198, 1, False)]
BOSS_LAYOUT = [("boss_phase", 0xA326, 2, False)]
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


def player_from_image(data) -> Player:
    """Deserialise the player object from the original byte image (bridge / verification only)."""
    data = getattr(data, "data", data)
    return Player(**{f: _rd(data, PLAYER_BASE, off, w, s) for f, off, w, s in PLAYER_LAYOUT})


def player_to_image(player: Player, data) -> None:
    """Serialise the player object back onto the original byte layout (bridge / verification only)."""
    data = getattr(data, "data", data)
    for f, off, w, _s in PLAYER_LAYOUT:
        _wr(data, PLAYER_BASE, off, w, getattr(player, f))


def rng_from_image(data) -> Rng:
    data = getattr(data, "data", data)
    return Rng(**{f: _rd(data, 0, off, w, s) for f, off, w, s in RNG_LAYOUT})


def rng_to_image(rng: Rng, data) -> None:
    data = getattr(data, "data", data)
    for f, off, w, _s in RNG_LAYOUT:
        _wr(data, 0, off, w, getattr(rng, f))


def globals_field_routing(image):
    """Build ``{PlayerGlobals-field-name: cluster-instance}`` for the scalar globals clusters (camera, motion,
    input, level_state, ..., the count==1 non-player/rng, non-EffectSlot routes), resolving each view field to
    the cluster instance that owns its OFFSET — so cluster-local dataclass name collisions (camera_script vs
    boss_script both have a ``script_ptr`` field, at different offsets) never mis-route. Bridge-only (uses
    offsets). Returns ``(instances, routing)`` — feed ``routing`` to NamedObjectBackend.register_fields to back
    the whole PlayerGlobals mega-view name-first."""
    from pre2.views.dgroup_view import PlayerGlobals
    data = getattr(image, "data", image)
    off_to_inst = {}
    instances = []
    for attr, cls, layout, base, count, stride in _ROUTES:
        if count != 1 or attr in ("player", "rng") or cls is EffectSlot:
            continue
        inst = _obj_from_image(cls, layout, data, base)
        instances.append(inst)
        for f, off, _w, _s in layout:
            off_to_inst[base + off] = (inst, f)
    routing = {}
    for name in dir(PlayerGlobals):
        desc = getattr(PlayerGlobals, name, None)
        off = getattr(desc, "off", None)
        if off is not None and off in off_to_inst:
            inst, dc_field = off_to_inst[off]
            if dc_field == name:                 # the cluster field must carry the SAME name the view uses
                routing[name] = inst
    return instances, routing


class NamedImageBackend:
    """Resolves a NAME-keyed view's fields (pre2/views/named_view) against the byte image via an offset layout.
    The DETACHABLE image resolver: it lets ONE name-keyed view definition serve the byte/verification path too
    (the shipped NamedObjectBackend serves the object path). It needs offsets, so it lives here in the bridge,
    never shipped. ``layout`` is the same ``(name, rel_off, width, signed)`` list the serialiser uses."""

    __slots__ = ("_data", "_base", "_map")

    def __init__(self, image, base, layout):
        self._data = getattr(image, "data", image)
        self._base = base
        self._map = {f: (off, w, s) for f, off, w, s in layout}

    def read_field(self, view, name, width, signed):
        off, _w, _s = self._map[name]
        return _rd(self._data, self._base, off, width, signed)

    def write_field(self, view, name, width, v):
        off, _w, _s = self._map[name]
        _wr(self._data, self._base, off, width, v)


def _obj_from_image(cls, layout, data, base=0):
    data = getattr(data, "data", data)
    vals = {}
    for f, off, w, s in layout:
        v = _rd(data, base, off, w, s)
        if f in _REF_FIELDS:                     # swizzle the raw offset -> an offset-free reference
            from pre2.bridge.pointer_layout import from_offset
            v = from_offset(v & 0xFFFF)
        vals[f] = v
    return cls(**vals)


def _obj_to_image(obj, layout, data, base=0):
    data = getattr(data, "data", data)
    for f, off, w, _s in layout:
        v = getattr(obj, f)
        if f in _REF_FIELDS:                     # swizzle the reference back to its exact 16-bit offset
            from pre2.bridge.pointer_layout import to_offset
            v = to_offset(v)
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


class DataclassBackend:
    """Run the game with named structures' live state as real offset-free dataclasses, not bytes.

    The north-star mechanism: ``NativeGameState.backend`` swaps to this and the gameplay tick runs UNCHANGED,
    but every read/write that lands in a routed structure's byte range is redirected — via the bridge layout —
    to/from the fields of a live dataclass (``self.player.x``, ``self.camera.col``, ``self.progress.lives``,
    ...). Everything else stays in the image. The offsets live ONLY here in the bridge mapping, not in the game
    logic or the store. Each structure added to ``_ROUTES`` shrinks the image's live surface. ``materialize``
    folds the objects back to bytes for the digest.
    """

    _IS_DGROUP_BACKEND = True
    __slots__ = ("_img", "_objs", "_map", "_arena", "_sparse", "_readonly_image", "_level_data")

    def __init__(self, seed, readonly_image=False):
        # readonly_image=True asserts the invariant that the gameplay tick writes NOTHING to the loaded data (all
        # mutable state is on the object graph); any fallback write then raises. ``_level_data`` is the tick's
        # own copy of the read-only loaded tables — so (objects + level_data) is a SELF-CONTAINED state of record
        # and the tick needs no external byte image at all (``_img`` is only a materialize target for the render).
        data = getattr(seed, "data", seed)
        self._img = data
        self._readonly_image = readonly_image
        self._level_data = bytearray(data[DGROUP_BASE:DGROUP_BASE + 0x10000])
        self._objs: dict[str, object] = {}
        # absolute dgroup offset -> (object, field, byte_index, width, signed) — mapped straight to the instance
        self._map: dict[int, tuple] = {}
        for attr, cls, layout, base, count, stride in _ROUTES:
            insts = [_obj_from_image(cls, layout, data, base + k * stride) for k in range(count)]
            self._objs[attr] = insts[0] if count == 1 else insts
            for k, inst in enumerate(insts):
                b0 = base + k * stride
                for f, off, w, s in layout:
                    for bk in range(w):
                        self._map[(b0 + off + bk) & 0xFFFF] = (inst, f, bk, w, s)
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
                    self._map[(i + off + bk) & 0xFFFF] = (e, f, bk, w, False)
            for k in range(st - 5):
                self._map[(i + 5 + k) & 0xFFFF] = (e, "body", k, 0, False)
            i += st
        self._objs["entities"] = [e for _, e in self._arena]
        # named working-memory buffers (raw bytearrays), mapped byte-for-byte (width 0 -> index into .data)
        for attr, base, ln in _BUFFERS:
            buf = ByteBuffer(attr, bytearray(data[DGROUP_BASE + base:DGROUP_BASE + base + ln]))
            self._objs[attr] = buf
            for k in range(ln):
                self._map[(base + k) & 0xFFFF] = (buf, "data", k, 0, False)
        # sparse working buffers (scattered offsets -> one bytearray, indexed by position)
        self._sparse = []
        for attr, offs in _SPARSE:
            offs = tuple(sorted(offs))
            buf = ByteBuffer(attr, bytearray(data[DGROUP_BASE + o] for o in offs))
            self._objs[attr] = buf
            self._sparse.append((offs, buf))
            for i, o in enumerate(offs):
                self._map[o & 0xFFFF] = (buf, "data", i, 0, False)

    # convenience accessors
    player = property(lambda self: self._objs["player"])
    rng = property(lambda self: self._objs["rng"])
    camera = property(lambda self: self._objs["camera"])
    progress = property(lambda self: self._objs["progress"])
    actors = property(lambda self: self._objs["actors"])
    entities = property(lambda self: self._objs["entities"])

    def rb(self, off: int) -> int:
        off &= 0xFFFF
        m = self._map.get(off)
        if m is None:                               # un-routed -> the read-only loaded tables
            return self._level_data[off]
        inst, f, k, w, _s = m
        if f in _REF_FIELDS:                        # a swizzled pointer: project the ref -> its offset, byte k
            from pre2.bridge.pointer_layout import to_offset
            return (to_offset(getattr(inst, f)) >> (8 * k)) & 0xFF
        if w == 0:                                  # a bytearray byte (record body / working buffer)
            return getattr(inst, f)[k]
        return (getattr(inst, f) & ((1 << (8 * w)) - 1)) >> (8 * k) & 0xFF

    def wb(self, off: int, val: int) -> None:
        off &= 0xFFFF
        val &= 0xFF
        m = self._map.get(off)
        if m is None:
            if self._readonly_image:
                raise AssertionError(f"gameplay tick wrote to the read-only loaded data at 0x{off:04X} — an "
                                     f"un-routed mutable byte (the object graph is not the complete store)")
            self._level_data[off] = val
            return
        inst, f, k, w, s = m
        if f in _REF_FIELDS:                        # a swizzled pointer: patch byte k of the offset, re-swizzle
            from pre2.bridge.pointer_layout import from_offset, to_offset
            cur = to_offset(getattr(inst, f))
            cur = (cur & ~(0xFF << (8 * k))) | (val << (8 * k))
            setattr(inst, f, from_offset(cur & 0xFFFF))
            return
        if w == 0:                                  # a bytearray byte (record body / working buffer)
            getattr(inst, f)[k] = val
            return
        v = getattr(inst, f) & ((1 << (8 * w)) - 1)
        v = (v & ~(0xFF << (8 * k))) | (val << (8 * k))
        if s and v & (1 << (8 * w - 1)):
            v -= 1 << (8 * w)
        setattr(inst, f, v)

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def materialize(self, data=None) -> None:
        # Self-contained: reconstruct the whole DGROUP from (loaded tables + the object graph), no external state.
        data = self._img if data is None else data
        data[DGROUP_BASE:DGROUP_BASE + 0x10000] = self._level_data
        for attr, _cls, layout, base, count, stride in _ROUTES:
            insts = self._objs[attr] if count > 1 else [self._objs[attr]]
            for k, inst in enumerate(insts):
                _obj_to_image(inst, layout, data, base + k * stride)
        for start, e in self._arena:
            b = DGROUP_BASE + start
            data[b] = e.stride & 0xFF
            data[b + 1] = e.flags1 & 0xFF
            data[b + 2] = e.sprite_ref & 0xFF
            data[b + 3] = (e.sprite_ref >> 8) & 0xFF
            data[b + 4] = e.skip & 0xFF
            data[b + 5:b + 5 + len(e.body)] = e.body
        for attr, base, ln in _BUFFERS:
            buf = self._objs[attr]
            data[DGROUP_BASE + base:DGROUP_BASE + base + ln] = buf.data
        for offs, buf in self._sparse:
            for i, o in enumerate(offs):
                data[DGROUP_BASE + o] = buf.data[i]
