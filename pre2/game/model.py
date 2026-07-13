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
    def anim_mirror(self) -> int:
        """The anim-mirror flag — ``facing``'s low byte (the DOS ``facing_lo`` alias) — derived."""
        return self.facing & 0xFF

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
    frame_blink: int = 0     # frame counter gating the trail emit / blink
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
