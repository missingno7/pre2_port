"""The SHIPPED game's memory model — plain dataclasses, exactly how you'd write the game from scratch.

This is the north star of the object-model milestone: the released game runs on THESE objects — real Python
fields (``player.x``, ``rng.lcg_a``), no DGROUP offsets, no byte image. Nothing here knows the original DOS
memory layout; that knowledge lives entirely in the DETACHABLE bridge (``pre2/bridge/game_layout.py``), which
serialises these objects to/from the original byte image ONLY when verifying against the DOS original. Detach
the bridge and this is a clean, independent game (and, by construction, it can no longer replay or snapshot a
byte image — those are bridge/VM concepts).

Canonical fields only: where the ASM reads the same bytes at two widths (``sprite``'s high byte is ``flags``;
``facing``'s low byte is the anim-mirror flag; ``death_state`` aliases the generic slot's ``life``), the model
stores ONE field and exposes the alias as a derived property — a real object graph has no redundant storage.
The bridge's serialiser is what re-projects these onto the overlapping original bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Player:
    """The player — position, kinematics, and animation state. No offsets; just the game's own fields."""

    x: int = 0            # world X, 12.4 fixed
    y: int = 0            # world Y, 12.4 fixed
    sprite: int = 0       # packed anim-frame word: (id & 0x1FFF) | flag bits
    xvel: int = 0         # X velocity, 12.4 fixed (signed)
    motion_mode: int = 0  # kinematics mode/shift (friction = 0xC >> mode)
    facing: int = 0       # +1 / -1 heading the FSM integrates with (signed)
    anim_b: int = 0       # anim B-state (anim-id memory; camera-shake gate input)
    anim_ptr: int = 0     # current anim-script cursor
    yvel: int = 0         # Y velocity, 12.4 fixed (signed)
    run_flag: int = 0     # run state (reset on an anim change)
    death_state: int = 0  # death/hurt state byte (0 = alive)

    @property
    def flags(self) -> int:
        """The sprite word's high byte (the DOS ``flags`` alias) — derived, not stored."""
        return (self.sprite >> 8) & 0xFF

    @property
    def facing_lo(self) -> int:
        """``facing``'s low byte — the anim-mirror flag the DOS view exposes as ``facing_lo``."""
        return self.facing & 0xFF

    anim_mirror = facing_lo   # readable alias of the same derived byte

    @property
    def life(self) -> int:
        """The generic render-slot ``life`` byte — for the player it IS ``death_state`` (same byte 0x11)."""
        return self.death_state

    @property
    def source(self) -> int:
        """The render-slot ``source`` word — for the player it aliases ``facing``'s bytes (unsigned)."""
        return self.facing & 0xFFFF

    @property
    def alive(self) -> bool:
        return self.death_state == 0


@dataclass
class Rng:
    """The game's random-number generators — the 4-byte LCG mixer + the 1-word rotate generator."""

    lcg_a: int = 0
    lcg_b: int = 0
    lcg_c: int = 0
    lcg_d: int = 0
    ror: int = 0


@dataclass
class Actor:
    """One entry of the 12-slot object/enemy list — a moving game actor. ``sprite == 0xFFFF`` means the slot is
    free. Real game object: position, velocity, animation, and combat state, no offsets."""

    x: int = 0
    y: int = 0
    sprite: int = 0xFFFF   # packed anim-frame word; 0xFFFF = empty slot
    def_ptr: int = 0       # -> the read-only type-definition record
    xvel: int = 0
    yvel: int = 0
    anim_ptr: int = 0      # the anim-script cursor
    state: int = 0         # behaviour state byte (0xFF = dead)
    hp: int = 0
    hits: int = 0          # hit accumulator
    life: int = 0          # anim/life counter

    @property
    def empty(self) -> bool:
        return self.sprite == 0xFFFF

    @property
    def flags(self) -> int:
        """The sprite word's high byte (the DOS ``flags`` alias)."""
        return (self.sprite >> 8) & 0xFF

    @property
    def source(self) -> int:
        """The render-slot ``source`` word (bytes +9/+10) — for an object slot it overlaps xvel-hi / yvel-lo."""
        return ((self.xvel >> 8) & 0xFF) | ((self.yvel & 0xFF) << 8)


@dataclass
class EffectSlot:
    """One slot of a projectile / burst / debris sprite list — a short-lived moving sprite. ``sprite == 0xFFFF``
    (free) or the alive flag in the sprite word's high byte gates it. Covers the whole 18-byte record; a couple
    of bytes (``aux_b``/``aux_10``) are record scratch the views don't individually name."""

    x: int = 0
    y: int = 0
    sprite: int = 0xFFFF
    xvel: int = 0
    kind: int = 0          # phase/kind byte
    source: int = 0        # back-reference to the spawning slot
    aux_b: int = 0
    anim_ptr: int = 0      # anim cursor / lifetime word
    yvel: int = 0
    aux_10: int = 0
    life: int = 0          # life / substate byte


@dataclass
class ByteBuffer:
    """A named working buffer — a contiguous region the tick scribbles as raw bytes (scenery-trigger scratch,
    level load buffers, camera-target scratch). Honest as bytes: it is transient working memory, not a record
    with fields, so it is modeled as a named bytearray rather than fake fields."""

    name: str
    data: bytearray = field(default_factory=bytearray)


@dataclass
class BonusCell:
    """One record of the 80-cell bonus/collectible list (stride 5) — cyxx `level_bonus_t`. The scan reads only
    ``cell`` (the packed x/y map position); the leading bytes are the level-init payload the scan doesn't
    interpret. Field names from cyxx/blues p2 (MAX_LEVEL_BONUSES 80 == our count)."""

    tile_num0: int = 0   # cyxx level_bonus_t.tile_num0
    tile_num1: int = 0   # cyxx level_bonus_t.tile_num1
    count: int = 0       # cyxx level_bonus_t.count
    cell: int = 0xFFFF   # packed (y_cell << 8) | x_cell; 0xFFFF = empty/collected. cyxx level_bonus_t.pos


@dataclass
class WallMarker:
    """One 8-byte wall-impact marker (the 20-slot table). ``token == 0x55AA`` means the slot is free; on a wall
    hit the collision code records the map offset + impact data in the remaining words."""

    token: int = 0x55AA
    map_off: int = 0x55AA
    data0: int = 0x55AA
    data1: int = 0x55AA

    @property
    def free(self) -> bool:
        return self.token == 0x55AA


@dataclass
class ArenaEntity:
    """One record of the variable-stride 2nd-pass entity list (0x8489). The header is named; ``body`` holds the
    handler-specific bytes past it. Entry 0 is the player. ``sprite_ref == 0xFFFF`` means empty."""

    stride: int            # record length in bytes
    flags1: int            # bit7 = off-screen cull, bits0-6 = handler index
    sprite_ref: int        # the sprite/anim reference word
    skip: int              # [+4] bit2 = skip
    body: bytearray = field(default_factory=bytearray)

    @property
    def handler_idx(self) -> int:
        return self.flags1 & 0x7F

    @property
    def empty(self) -> bool:
        return self.sprite_ref == 0xFFFF


@dataclass
class Camera:
    """The scrolling camera — its cell column/row and the fine sub-cell scroll state."""

    col: int = 0          # camera cell column (the level-map X the viewport starts at)
    row: int = 0          # camera cell row
    fine_scroll: int = 0  # sub-cell pixel scroll
    row_factor: int = 0   # the row-stride factor the renderer multiplies by

    # read/write aliases of the DOS view's field names (cam_col_word/cam_col are the same word as `col`)
    @property
    def cam_col_word(self) -> int:
        return self.col

    @cam_col_word.setter
    def cam_col_word(self, v: int) -> None:
        self.col = v

    cam_col = cam_col_word

    @property
    def cam_row_word(self) -> int:
        return self.row

    @cam_row_word.setter
    def cam_row_word(self, v: int) -> None:
        self.row = v

    cam_row = cam_row_word


@dataclass
class Progress:
    """The player's run progress / HUD state — scattered across the DOS globals, one object here."""

    score_lo: int = 0
    score_hi: int = 0     # score is the 32-bit (score_hi << 16 | score_lo)
    lives: int = 0
    energy: int = 0       # hearts (0..3)
    level: int = 0        # current level number
    bonus_letters: int = 0  # the collected BONUS-letters bitmask
    utensils_mask: int = 0  # the collected utensils/tools bitmask

    @property
    def score(self) -> int:
        return (self.score_hi << 16) | self.score_lo


@dataclass
class Input:
    """The decoded per-frame input state the tick reads (directions + fire + the demo/live source)."""

    up: int = 0
    down: int = 0
    left: int = 0
    right: int = 0
    fire: int = 0
    source: int = 0       # 0 = live keyboard, 1 = demo playback

    # read/write aliases of the DOS view's field names
    @property
    def in_up(self) -> int:
        return self.up

    @in_up.setter
    def in_up(self, v: int) -> None:
        self.up = v

    @property
    def in_down(self) -> int:
        return self.down

    @in_down.setter
    def in_down(self, v: int) -> None:
        self.down = v

    @property
    def in_left(self) -> int:
        return self.left

    @in_left.setter
    def in_left(self, v: int) -> None:
        self.left = v

    @property
    def in_right(self) -> int:
        return self.right

    @in_right.setter
    def in_right(self, v: int) -> None:
        self.right = v

    @property
    def in_fire(self) -> int:
        return self.fire

    @in_fire.setter
    def in_fire(self, v: int) -> None:
        self.fire = v

    @property
    def input_source(self) -> int:
        return self.source

    @input_source.setter
    def input_source(self, v: int) -> None:
        self.source = v


@dataclass
class Motion:
    """The player's gravity / fall / animation-gate state + the small per-frame timers the FSM ticks."""

    airborne: int = 0       # no ground under the player
    fall_frames: int = 0    # descending-fall counter
    fall_latch: int = 0     # fall-started latch
    fall_grace: int = 0     # coyote-time grace
    low_gravity: int = 0    # low-gravity / attack-invuln flag
    fly_timer: int = 0
    idle_timer: int = 0     # idle/fidget clock gate
    anim_gate: int = 0      # hold-current-anim / FSM-route gate
    charge: int = 0
    hurt_cooldown: int = 0


@dataclass
class PlayerState:
    """More of the player FSM / motion state — the glider, landing, input helpers, and per-frame scratch."""

    trail_ring: int = 0      # landing-dust / trail effect ring cursor
    glider: int = 0          # glider / flying gate
    fly_hold: int = 0        # glider hold budget
    last_land_y: int = 0     # Y of the last landing (fall-height source)
    input_suppress: int = 0  # nonzero forces the input bitmask to 0
    anim_hi: int = 0         # advance_anim's raw frame high byte
    frame_blink: int = 0     # frame counter gating the trail emit / blink; DOS view name: frame_stamp

    @property
    def frame_stamp(self) -> int:
        return self.frame_blink

    @frame_stamp.setter
    def frame_stamp(self, v: int) -> None:
        self.frame_blink = v
    input_lr: int = 0        # left|right held
    input_ud: int = 0        # up|down held
    drop_gate: int = 0       # nonzero: drop-through tiles active
    scale_level: int = 0     # object_update's sprite scale level
    camera_shake: int = 0    # = 8 on a hard fall -> camera shake
    run_count: int = 0       # inc-wrap run counter
    friction: int = 0        # per-level directional-friction constant


@dataclass
class Scroll:
    """The scripted-camera scroll + palette-fade / lighting state."""

    bonus_flash: int = 0     # bonus-collect flash timer
    to_dark: int = 0         # fade toward the dark palette
    to_light: int = 0        # fade back toward the level palette
    step: int = 0            # the fade ramp step
    lights_off: int = 0      # resting state after a lights-off fade
    phase: int = 0           # camera/scroll phase counter
    vx: int = 0              # scroll-cursor X velocity
    vy: int = 0              # scroll-cursor Y velocity
    script_last: int = 0     # camera-script pointer last seen

    @property
    def scroll_phase(self) -> int:
        return self.phase

    @scroll_phase.setter
    def scroll_phase(self, v: int) -> None:
        self.phase = v

    @property
    def scroll_vx(self) -> int:
        return self.vx

    @scroll_vx.setter
    def scroll_vx(self, v: int) -> None:
        self.vx = v

    @property
    def scroll_vy(self) -> int:
        return self.vy

    @scroll_vy.setter
    def scroll_vy(self, v: int) -> None:
        self.vy = v


@dataclass
class LevelState:
    """The level / transition state — end mode, respawn + end signals, checkpoint, and redraw flags."""

    flags: int = 0
    end_mode: int = 0      # 0 gameplay, 1 normal end, >1 warp
    respawn_state: int = 0
    end_signal: int = 0
    checkpoint_x: int = 0
    checkpoint_y: int = 0
    grid_dirty: int = 0

    @property
    def level_flags(self) -> int:
        return self.flags

    @level_flags.setter
    def level_flags(self, v: int) -> None:
        self.flags = v

    @property
    def level_end_mode(self) -> int:
        return self.end_mode

    @level_end_mode.setter
    def level_end_mode(self, v: int) -> None:
        self.end_mode = v


@dataclass
class AttackState:
    """The player's club-attack phase + the glider tilt (adjacent DOS bytes, same combat-input family)."""

    attack_phase: int = 0    # index into the 5-byte attack-phase record table
    attack_v19: int = 0      # the phase's projectile damage/tolerance value
    glider_tilt: int = 3     # glider tilt/pitch 0..6, neutral 3


@dataclass
class HitScratch:
    """Per-frame hitbox/quake scratch the combat + boss-quake code shares."""

    quake_dist_lo: int = 0   # boss-quake player-distance^2, low word
    quake_dist_hi: int = 0   # ... high word (must be 0 for the proximity test)
    hit_pass_full: int = 0   # set across a pass -> hitbox_overlap uses the FULL (un-halved) tolerance
    hit_flag: int = 0        # hitbox_overlap's vertical-detail hit flag
    hit_detail: int = 0      # vertical penetration depth when hit_flag is set


@dataclass
class SpawnCursor:
    """The level-spawn / effect-burst cursor state."""

    spawn_count: int = 0       # the level spawn param (>>3 = spawn count; boss damage decrements)
    cam_state: int = 0         # the camera sequencer's 8-state machine state (0xFF = disabled)
    cursor_x: int = 0          # the spawn/camera cursor position
    cursor_y: int = 0
    burst_x: int = 0           # spawn_effect_burst origin X
    burst_y: int = 0           # ... origin Y
    burst_sprite: int = 0      # ... the burst sprite id
    spawned_ptr: int = 0       # the just-spawned burst-slot pointer
    anim_ready: int = 0        # object_update's per-step anim-ready scratch byte
    spawn_offset_ring: int = 0  # 16-slot ring index into the spawn X-offset table


@dataclass
class CameraScript:
    """The scripted-camera sequencer: its per-state timer, the live script cursor, and the 4 target-record
    pointers the script positions the camera against."""

    cam_timer: int = 0         # the sequencer's per-state frame timer
    cmd_byte: int = 0          # the camera-script command byte (bit6 = a vertical nudge)
    dist_dir: int = 0          # 1 if the cursor is left of the player
    dist_x: int = 0            # |player_X - cursor_X|
    hit_debounce: int = 0      # frame stamp of the last camera-target hit (debounce window)
    script_cursor: int = 0     # the live camera-script cursor (into the bytecode)
    script_ptr: int = 0        # the active camera-script pointer (script_last detects changes)
    cursor_latch_x: int = 0    # cursor pos latched per script command
    cursor_latch_y: int = 0
    cam_param_e: int = 0       # the 5th camera-target param word (no position pair)
    cam_target_ptr: int = 0    # camera target record-ptr latch
    target_a: int = 0          # camera target record-ptr A (free sprites that hit it)
    target_b: int = 0          # camera target record-ptr B


@dataclass
class SceneryState:
    """Scattered level/scenery bookkeeping: the map bound, sagging-bridge tracking, the FSM's current-object
    pointer, redraw flags, the sprite-ref rebase banks, and the render-mirror display page."""

    map_rows: int = 0            # the map's bottom row bound
    dipping_tile: int = 0        # map offset of the currently-sagging bridge tile; 0x55AA = none
    current_object: int = 0      # the FSM's current-object pointer; NULL outside object-vs-object collision
    page_dirty: int = 0          # one-tile direct re-blit page flag
    grid_dirty_token: int = 0    # the whole-grid-dirty companion token (0x55AA)
    col_ring: int = 0            # the background column ring index
    sprite_bank_lo: int = 0      # entity sprite-ref rebase bank base A
    sprite_bank_hi: int = 0      # ... bank base B
    firefly_scratch_a: int = 0   # the firefly pass's per-frame scratch pair
    firefly_scratch_b: int = 0
    collected_linked: int = 0    # the LINKED-item collected count
    collected_counter: int = 0   # bumped once per bonus collected (the tally-percent numerator)
    display_page: int = 0        # the CRTC display-start page the present flips
    cam_left: int = 0            # camera-left tile — the X-integrate right bound


@dataclass
class AttractState:
    """The attract-mode / demo-header state."""

    attract_mode: int = 0     # the attract-demo header's mode byte
    attract_level: int = 0    # the attract/default level header
    in_aux: int = 0           # the sixth input flag (single scancode source) — idle-gate input
    idle_clock: int = 0       # the PIT-fed idle counter (the fidget selector reads &0x1FF)


@dataclass
class Boss:
    """The level-6/boss fight phase. ``boss_x``/``boss_y`` (the boss's own position) are NOT duplicated here —
    they physically overlay ``target_records[0].x``/``.y`` (a DOS memory alias, two names for one word), so
    they are already real fields, just reached through the camera-target array rather than this dataclass."""

    boss_phase: int = 0   # advances every 7 hits


@dataclass
class BossScript:
    """The mode-9 boss glyph-script interpreter state (a separate boss subsystem from ``Boss``/``boss_phase``
    — these bytes are addressed by object_spawn.py's own local constants, not a PlayerGlobals view name)."""

    script_ptr: int = 0xFFFF   # the live boss-script cursor (== the mode-9 init flag; -1 = not yet seeded)
    dwell: int = 0             # the dwell counter (decremented by jump opcodes; the advance fires at 0)
    cycle: int = 0             # hit cadence counter (&3); every 4th hit switches scripts
    m9_ptr: int = 0            # the relative-wrap script-table pointer
    m9_count: int = 0          # spawn-count seed / boss health (saturating; 0 = boss dead)


@dataclass
class DifficultyMode:
    """The BEGINNER/EXPERT difficulty toggle."""

    mode: int = 0        # 0 = BEGINNER / 1 = EXPERT
    mode_copy: int = 0   # the committed copy the loader reads
