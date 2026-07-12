"""The object/effect spawner pre-amble (1030:6822) — the head that falls through into 684E (object_tick).

Run once per frame from the main-loop spine (0x0220), before the object-update walker. It dispatches three
game-mode-gated event/spawn routines, all of which call the shared list-initialiser 7585:

  * 70D7  if [0x91FE]!=0xFF   — the terrain-entity spawner (streams the 0x9107 list 4907 updates)
  * 6D34  if [0x2D8A]==5      — a mode-5 player-interaction event (unwitnessed)
  * 6ADD  if [0x2D8A]==9      — a mode-9 spawn event

Recovered leaf-first. ``rw``/``rb`` read DS words/bytes; each transform returns a ``{offset: (value, width)}``
write contract (the bridge applies it). Audio is a side-effect boundary: 7585 plays sound 0xD via 0x2CC, which
the live hook emits separately (like the other play_sfx seams) — it is outside the recovered list windows.
"""
from __future__ import annotations

from pre2.islands import oracle_link
from pre2.recovered.combat_interaction import hitbox_overlap, roll_bonus_sprite, spawn_effect_burst
from pre2.recovered.prng import rng_lcg
from pre2.views.tables import Tables
from pre2.views.dgroup_view import (DictBackend, EffectParticle, PlayerGlobals, PlayerView, RenderSlot,
                                    RngView, WidthContractBackend)


class Pre2SpawnGap(Exception):
    """An unrecovered path in the 6822/70D7 spawner machinery (fail loud; never silently run ASM)."""


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _sar16(v, n):
    return (_s16(v) >> n) & 0xFFFF


# --- 7585: the 8-slot effect-row list at 0x56A2 (a horizontal row of sprite-0x135 effects + a sound) ---
EFFECT_ROW_LO = 0x56A2
EFFECT_ROW_N = 8             # [asm 758E] cap
EFFECT_ROW_STRIDE = 0x12
EFFECT_SPRITE = 0x135
EFFECT_X0 = 0xD              # [asm 7599] X start (ax, preserved across the 0x2CC sound call)
EFFECT_DX = 5               # [asm 75A9]
EFFECT_Y = 0xAA             # [asm 758B] dx


# Sentinel key in the writes dict (like the player FSM's SCROLL_REQUEST): [asm 7599-759C] when cx>0, 7585
# loads SONG 0xD (MONSTER.TRK, the BOSS MUSIC) via 02CC before writing the boss health-bar row. The adapters
# POP it (never a DGROUP write): native -> state.song_request; hybrid -> run the real 02CC with ax=0xD.
SONG_REQUEST = "__song"
BOSS_SONG_INDEX = 0xD       # [asm 7599 mov ax,0xd] the 02CC song index (MONSTER.TRK)
# Sentinel: the mode-9 boss GLYPH id the script interpreter selected this tick ([asm 6BD3] AL at the 6C0D
# call). The renderer needs it to draw the boss image (6C0D), and it is NOT reliably derivable from the
# post-tick script cursor — so the interpreter latches it into the contract; adapters pop it.
GLYPH_LATCH = "__boss_glyph"


@oracle_link("1030:7585",
             "the shared effect-row list-init: spawn min(cx,8) sprite-0x135 effects into the 8-slot list "
             "0x56A2 (stride 0x12) as a horizontal row — [+4]=0x135, [+0]=X (0xD, step 5), [+2]=Y (0xAA) — "
             "then fill the remaining slots with the 0xFFFF terminator. ``cx`` is the spawn count from the "
             "caller (70D7/6ADD/6D34). When cx>0 it FIRST loads song 0xD (MONSTER.TRK, the boss music) via "
             "0x2CC [asm 7599-759C] — returned as the SONG_REQUEST sentinel, popped by the adapters.",
             "OBSERVED", merge_target="object_spawn")
def init_effect_row(cx):
    """[asm 7585] ``cx`` = the caller's spawn count. Returns the ``{offset: (value, width)}`` 0x56A2 contract
    (+ the ``SONG_REQUEST`` sentinel when cx>0 — the boss-music load the adapters emit)."""
    be = WidthContractBackend(lambda o: 0, lambda o: 0)  # write-only: the row slots fill the contract
    row = PlayerGlobals(be).effect_row
    writes = be.writes
    n = cx if cx < EFFECT_ROW_N else EFFECT_ROW_N        # [asm 758E-7593] cap at 8
    x = EFFECT_X0
    if n:                                                # [asm 7597 jcxz / 7599-759C] boss music on a live row
        writes[SONG_REQUEST] = BOSS_SONG_INDEX
    for i in range(n):                                   # [asm 759F-75AF] the spawned row
        row[i].sprite = EFFECT_SPRITE
        row[i].x = x
        row[i].y = EFFECT_Y
        x = (x + EFFECT_DX) & 0xFFFF
    for i in range(n, EFFECT_ROW_N):                     # [asm 75B9-75C1] terminate the rest
        row[i].sprite = 0xFFFF
    return writes


# --- 757A: a leaf of the 70D7 camera/scroll state machine (states 1/2 advance the scroll-phase counter) ---
SCROLL_PHASE = 0x6C05


@oracle_link("1030:757A",
             "saturating increment of the scroll-phase counter [0x6C05]: add 1, then clamp at 0xFF (the "
             "`add [0x6C05],1` / `sbb [0x6C05],0` idiom). A leaf of the 70D7 camera/scroll state machine.",
             "OBSERVED", merge_target="object_spawn")
def inc_scroll_phase(rb):
    """[asm 757A] ``rb(off)`` reads a DGROUP byte. Returns the ``{offset: (value, width)}`` write contract."""
    v = rb(SCROLL_PHASE)
    return {SCROLL_PHASE: ((0xFF if v == 0xFF else v + 1), 1)}


# --- 80DE/8182: the camera-target collision scan (a 70D7 tail leaf, run every frame) ---
SCAN_PLAYER = 0x4F0A        # the player sprite record (combat record)
SCAN_PROJ = 0x4F2E          # the 4 thrown-weapon projectile slots
SCAN_PROJ_N = 4
TARGET_A = 0xA423           # camera target record-ptr A (free 0x19C/0x19D sprites that hit it)
TARGET_B = 0xA425           # camera target record-ptr B
TARGET_SPRITES = (0x19C, 0x19D)


def _target_collision(rb, rw, si, writes):
    """[asm 8182] test one sprite ``si`` against the camera targets; accumulate the hitbox + free writes into
    ``writes``; return the ASM's CF at the RET.

    Two paths, and only ONE signals a "response" hit to the caller (scan_camera_targets fires its scroll
    response iff this returns True):
      * TARGET-A [asm 819C]: if target A's OWN id is a 0x19C/0x19D breakable, a hitbox overlap FREES the
        projectile ([si+4]=0xFFFF) and then ``jmp 81B2 -> clc -> ret`` = CF **0 (False)** — the projectile is
        consumed but this is NOT a response hit. (A wrong True here fired the scroll response on every level-9
        breakable — underflowing [0x91FC] into the state-6 dive; finish-game demo tick 24.)
      * TARGET-B [asm 81A8-81AF]: the overlap CF is returned as-is (81AF ret) — THIS is the response hit
        (the boss/target that decrements [0x91FC]; the gorilla damages through here, not target A)."""
    be = WidthContractBackend(rb, rw, writes)
    slot = RenderSlot(be, si)                             # the probing sprite/projectile record
    if slot.sprite == 0xFFFF:                             # [asm 8182/8186 je 81B2] inactive -> clc (False)
        return False
    di = rw(TARGET_A)                                     # [asm 8188] di = [0xA423] (the target record ptr)
    target_a = RenderSlot(be, di)
    if (target_a.sprite & 0x1FFF) in TARGET_SPRITES:      # [asm 818C-819A] TARGET_A's OWN id is 0x19C/0x19D
        hit, hb = hitbox_overlap(rb, rw, si, di)             # [asm 819C] 8D7B (si vs target A)
        for off, (val, wid) in hb.items():
            writes[off] = (val, wid)
        if hit:                                          # [asm 819F jae 81A8 / 81A1-81A6]
            slot.sprite = 0xFFFF                          # free the projectile, then jmp 81B2 -> clc:
            return False                                  #   consumed, but NOT a response hit
    hit, hb = hitbox_overlap(rb, rw, si, rw(TARGET_B))   # [asm 81A8-81AC] target B -> 81AF ret (CF as-is)
    for off, (val, wid) in hb.items():
        writes[off] = (val, wid)
    return hit


FLASH_RECORDS = (0x564D, 0x565F, 0x5671, 0x5683, 0x5695)   # the 5 camera-target records' sprite-id high byte


@oracle_link("1030:80DE",
             "the camera-target collision scan + response (run every frame from the 70D7 tail): test the player "
             "(0x4F0A) then the 4 projectile slots (0x4F2E) against the camera targets [0xA423]/[0xA425] via 8D7B "
             "(freeing a 0x19C/0x19D sprite that overlaps target A). On a hit, and unless debounced "
             "(|[0x6BD5]-[0xA3FD]|<=0x16), run the response: latch [0xA3FD], sfx 8, set the flash bit6 on all 5 "
             "target records, [0x6BC5]=0, inc_scroll_phase (757A), then decrement the level param [0x91FC] by "
             "[0x7B19] -> state 6 + dive [0x6C08] on underflow, else a state-5 scroll kick when [0x7B19]>0x14; "
             "[0xA3F7]=0 and free the hitting slot. Composes hitbox_overlap + inc_scroll_phase.",
             "OBSERVED", merge_target="object_spawn")
def scan_camera_targets(rb, rw):
    """[asm 80DE..8181] ``rb``/``rw`` read DGROUP byte/word. Returns the ``{offset: (value, width)}`` contract.
    The sfx-8 emit is an audio side-effect seam (not in the state contract)."""
    writes: dict[int, tuple[int, int]] = {}
    g = PlayerGlobals(WidthContractBackend(rb, rw, writes))
    si_hit = None
    if _target_collision(rb, rw, SCAN_PLAYER, writes):   # [asm 80DE-80E4] player first
        si_hit = SCAN_PLAYER
    else:
        for slot in g.projectiles:                        # [asm 80E6-80F4] then the 4 projectiles
            if _target_collision(rb, rw, slot.offset, writes):
                si_hit = slot.offset
                break
    if si_hit is None:                                    # [asm 80F6] no hit
        return writes

    bd5 = g.frame_stamp                                   # [asm 80F7] the frame stamp (the 0x6BD5 word)
    if _abs16((bd5 - g.hit_debounce) & 0xFFFF) <= 0x16:   # [asm 80FC-8107] debounce on the last-hit time
        return writes
    g.hit_debounce = bd5                                  # [asm 8109]
    # [asm 8110] play_sfx 8 -> audio seam (emitted at the live hook, not part of the state contract)
    for tgt in g.boss_targets:                            # [asm 8113-8127] set the flash bit on every target
        tgt.flags = tgt.flags | 0x40
    g.glider = 0                                          # [asm 812C]
    writes.update(inc_scroll_phase(rb))                  # [asm 8131] 757A
    al = g.attack_v19                                     # [asm 8134]
    nfc = (rw(SPAWN_COUNT) - al) & 0xFFFF                 # [asm 8139] decrement the level param
    writes[SPAWN_COUNT] = (nfc, 2)
    if nfc & 0x8000:                                      # [asm 813D] underflow -> finale approach
        writes[CAM_STATE] = (6, 1)                        # [asm 813F]
        writes[SCROLL_VY] = (0xFF80, 2)                   # [asm 8144]
        return writes
    if al > 0x14:                                         # [asm 814C] a scroll kick
        dx = 0x30 if _s16(rw(CURSOR_X)) >= _s16(rw(PLAYER_X)) else (-0x30) & 0xFFFF   # [asm 8153-815C]
        writes[SCROLL_VX] = (dx, 2)                       # [asm 815E]
        writes[SCROLL_VY] = (0xFF80 if _s16(rw(SCROLL_VY)) > 0 else 0xFFC0, 2)        # [asm 8162-816E]
        writes[CAM_STATE] = (5, 1)                        # [asm 8171]
    writes[0xA3F7] = (0, 2)                               # [asm 8176]
    RenderSlot(WidthContractBackend(rb, rw, writes), si_hit).sprite = 0xFFFF   # [asm 817C] free the hitting slot
    return writes


# --- 70D7 head (70D7..7172): the spawn gate + scroll-cursor advance (the every-frame core of the engine) ---
SPAWN_COUNT = 0x91FC       # >>3 = spawn count
SPAWN_GATE_PTR = 0x564C    # di; -1 or bit 0x2000 clear -> no spawn
CURSOR_X = 0x91FF
CURSOR_Y = 0x9201
CURSOR_X_LO = 0x91F7       # level X bounds for the cursor
CURSOR_X_HI = 0x91F9
SCROLL_VX = 0x6C06
SCROLL_VY = 0x6C08
SCROLL_STOP_FLAG = 0x6BEA
CAM_STATE = 0x91FE         # the camera state-machine state (==6 suppresses the spawn)
SCROLL_GRAVITY = 0x10
SCROLL_VY_GRAV_CAP = 0xE0


@oracle_link("1030:70D7",
             "the 70D7 head (the camera/scroll engine core, run every frame): a spawn gate (cx=[0x91FC]>>3, "
             "zeroed unless [0x564C]!=-1 & [0x91FE]!=6 & [0x564C]&0x2000) feeding 7585; then advance the spawn "
             "cursor [0x91FF] by Vx>>4 (clamped to [0x91F7]..[0x91F9]) and [0x9201] by Vy>>4; when scrolling "
             "down/still (Vy>=0) and the cursor tile is solid (es=[0x2DDA], table [0x7F5E]) snap [0x9201] to the "
             "tile row + stop the scroll ([0x6C08]=[0x6C06]=0, [0x6BEA]=7); when scrolling up apply gravity "
             "[0x6C08]+=0x10. Composes init_effect_row (7585); contract verified at the 0x7172 boundary.",
             "OBSERVED", merge_target="object_spawn")
def tick_scroll_cursor(rb, rw, read_tile):
    """[asm 70D7..7172] ``rb``/``rw`` read DGROUP byte/word; ``read_tile`` reads the level map (es=[0x2DDA])."""
    writes: dict[int, tuple[int, int]] = {}
    di = rw(SPAWN_GATE_PTR)                                # [asm 70E1-70F7] spawn gate
    cx = (rw(SPAWN_COUNT) >> 3) & 0xFFFF
    if di == 0xFFFF or rb(CAM_STATE) == 6 or not (di & 0x2000):
        cx = 0
    writes.update(init_effect_row(cx))                    # [asm 70F9] 7585

    ax = (_sar16(rw(SCROLL_VX), 4) + rw(CURSOR_X)) & 0xFFFF   # [asm 70FE-7103] advance cursor X
    if not (ax & 0x8000) and _s16(rw(CURSOR_X_LO)) <= _s16(ax) and _s16(rw(CURSOR_X_HI)) >= _s16(ax):
        writes[CURSOR_X] = (ax, 2)                        # [asm 7115] within bounds
        cur_x = ax
    else:
        cur_x = rw(CURSOR_X)

    cur_y = (rw(CURSOR_Y) + _sar16(rw(SCROLL_VY), 4)) & 0xFFFF   # [asm 7118] advance cursor Y
    writes[CURSOR_Y] = (cur_y, 2)
    vy = rw(SCROLL_VY)
    landed = False
    if _s16(vy) >= 0:                                     # [asm 7121-7126] jl 7165 (scrolling up -> straight to gravity)
        col = _sar16(cur_x, 4) & 0xFF
        row = _sar16(cur_y, 4) & 0xFF
        tile = read_tile((col | (row << 8)) & 0xFFFF)     # [asm 7128-713B]
        if Tables(rb).floor_props[tile] != 0:             # [asm 7142-7144] solid floor -> land
            writes[CURSOR_Y] = ((cur_y & 0xFFF0), 2)      # [asm 7146] snap (and byte [0x9201],0xF0)
            if _s16(vy) != 0:                             # [asm 714B] was scrolling
                writes[SCROLL_STOP_FLAG] = (7, 1)
            writes[SCROLL_VY] = (0, 2)                     # [asm 7157] stop the scroll
            writes[SCROLL_VX] = (0, 2)
            landed = True
    if not landed and _s16(vy) < SCROLL_VY_GRAV_CAP:      # [asm 7165] fall faster toward the floor (cap 0xE0)
        writes[SCROLL_VY] = ((vy + SCROLL_GRAVITY) & 0xFFFF, 2)
    return writes


# --- 7172..71AB: the player-vs-cursor distance that gates the camera state machine ---
PLAYER_X = 0x4F1C
PLAYER_Y = 0x4F1E
DIST_DIR = 0xA3FA          # 1 if the cursor is left of the player
DIST_X = 0xA3FB            # |player_X - cursor_X|
CULL_X = 0x190
CULL_Y = 0xFA


def _abs16(v):
    v &= 0xFFFF
    return (-v) & 0xFFFF if v & 0x8000 else v


@oracle_link("1030:7172",
             "the player-vs-cursor distance (70D7, between the scroll-cursor head and the camera state "
             "machine): [0xA3FA] = 1 if the cursor [0x91FF] is left of the player [0x4F1C] else 0; [0xA3FB] = "
             "|player_X - cursor_X|. Returns the writes + a cull flag: the camera state machine is skipped "
             "(jmp 7579) when |player_X-cursor_X| > 0x190 or |player_Y-cursor_Y| > 0xFA.",
             "OBSERVED", merge_target="object_spawn")
def player_cursor_dist(rw):
    """[asm 7172..71AB] ``rw`` reads a DGROUP word. Returns ``(writes, cull)``."""
    px = rw(PLAYER_X)
    cur_x = rw(CURSOR_X)
    dir_flag = 0 if cur_x >= px else 1                    # [asm 7177] jae (unsigned)
    xd = _abs16((px - cur_x) & 0xFFFF)                    # [asm 7185-718B]
    writes = {DIST_DIR: (dir_flag, 1), DIST_X: (xd, 2)}
    cull = xd > CULL_X                                    # [asm 7190] jbe
    if not cull:
        yd = _abs16((rw(PLAYER_Y) - rw(CURSOR_Y)) & 0xFFFF)   # [asm 7198-71A1]
        cull = yd > CULL_Y                               # [asm 71A3] jbe
    return writes, cull


# --- 824D: the player-hurt effect at the bottom of the camera-boundary crush chain (81B4->81F3->824D) ---
HURT_COOLDOWN = 0x6BC9    # damage cooldown; reloads to 5 and costs energy on underflow
ENERGY = 0x27D6           # hit-points within a life (= PlayerGlobals.energy; was misnamed LIVES here)
HURT_FX_X = 0xA336        # spawn_effect_burst reads this as its SPAWN_X
HURT_FX_Y = 0xA338        # ... SPAWN_Y (player_Y - 0x30)
HURT_FX_SPRITE = 0xA33A   # ... BURST_SPRITE (0x2046)


@oracle_link("1030:65B3",
             "player death, armed by 824D when energy [0x27D6] underflows (the scroll-boundary crush chain). This "
             "is the SAME 65B3 the 5A96 ground/off-camera death sites reach, so it delegates to the single canonical "
             "recovered copy (player_collision._offcamera_trigger) and adapts its plain-byte writes to the "
             "object_spawn (value, width) contract: consume a life [0x27D8] + reset energy [0x27D6]=0 + arm the "
             "death->respawn dispatch [0x6BE4]=2, or arm game-over [0x6BE5]=1 when out of lives. The 4C69 "
             "level-state machine runs the actual death-bounce / respawn / game-over (native: native_level_state -> "
             "Pre2RespawnTransition / Pre2GameOverTransition).",
             "OBSERVED", merge_target="object_spawn")
def player_death(rb, rw):
    """[asm 65B3] Returns the ``{offset: (value, width)}`` writes that arm the death/respawn/game-over dispatch.
    Thin adapter over the canonical 65B3 (``player_collision._offcamera_trigger``) — single source of the logic."""
    from pre2.recovered.player_collision import _offcamera_trigger
    return {o: (v, 1) for o, v in _offcamera_trigger(rb).items()}


@oracle_link("1030:824D",
             "the player-hurt effect when the scroll boundary crushes the player (bottom of the 81B4 chain): "
             "tick the damage cooldown [0x6BC9] (reload 5 + spend energy [0x27D6] on underflow -> player death "
             "65B3 when lives deplete), record the hurt-burst origin [0xA336]/[0xA338] = player - (0,0x30), then "
             "spawn one hurt sprite (8D1B, id 0x2046, Xvel +/-0x30 by [0x6BC9] parity, Yvel 0xFF80). Composes "
             "the verified spawn_effect_burst over a read-through overlay so it sees the just-set origin/sprite.",
             "OBSERVED", merge_target="object_spawn")
def hurt_effect(rb, rw):
    """[asm 824D] Returns the ``{offset: (value, width)}`` writes. On the lives-depleted death (energy underflow)
    it composes ``player_death`` (65B3) to arm the 4C69 death/respawn dispatch — which resolves to the recovered
    game-over; no fail-loud."""
    writes = {}
    cd = (rb(HURT_COOLDOWN) - 1) & 0xFF
    if cd & 0x80:                                        # [asm 8254] dec underflowed -> reload + lose a life
        writes[HURT_COOLDOWN] = (5, 1)
        cd = 5
        energy = (rb(ENERGY) - 1) & 0xFF
        writes[ENERGY] = (energy, 1)
        if energy & 0x80:                                # [asm 825F] energy underflow -> 65B3 player death
            writes.update(player_death(rb, rw))          # [asm 8261] arm the 4C69 death/respawn dispatch
    else:
        writes[HURT_COOLDOWN] = (cd, 1)
    writes[HURT_FX_X] = (rw(PLAYER_X), 2)                # [asm 8264]
    writes[HURT_FX_Y] = ((rw(PLAYER_Y) - 0x30) & 0xFFFF, 2)   # [asm 826A]
    ax = (-0x30 if (cd & 1) else 0x30) & 0xFFFF          # [asm 8276-8280] sign by the final [0x6BC9] parity
    writes[HURT_FX_SPRITE] = (0x2046, 2)                 # [asm 8285]
    orw = lambda o: (writes[o & 0xFFFF][0] & 0xFFFF) if (o & 0xFFFF) in writes else rw(o)   # 8D1B reads the writes above
    writes.update(spawn_effect_burst(rb, orw, ax, 0xFF80, 1))    # [asm 828B]
    return writes


# --- 81F3: one camera-boundary target vs the player (the per-target half of the 81B4 crush) ---
SCROLL_PUSH = 0x91FB      # al = 4 - [0x91FB] is subtracted from the scroll phase on a bounce
PLAYER_XVEL = 0x4F22
PLAYER_YVEL = 0x4F2A


@oracle_link("1030:81F3",
             "one camera-boundary target `di` vs the player (the per-target half of 81B4): skip unless the "
             "target is active ([di+4]!=0xFFFF) and a boundary ([di+5]&0x20); test the player hitbox (8D7B). On "
             "overlap, knock the scroll phase [0x6C05] down by 4-[0x91FB] (clamped to 0), hurt the player "
             "(824D), and bounce: [0x4F2A]=0xFF80 (up), [0x4F22]=+/-0x80 (away from the cursor), facing "
             "[0x4F24]=3, plus [0x4F2D]=0x2C / [0x6BD0]=[0x6BC7]=0. Plays sfx 1 (audio seam, not in the state "
             "contract). Composes the verified hitbox_overlap + hurt_effect.",
             "OBSERVED", merge_target="object_spawn")
def camera_target_bounce(rb, rw, di):
    """[asm 81F3] ``di`` = the camera-target record offset. Returns the ``{offset: (value, width)}`` writes
    (8D7B always contributes [0xA330]); the lives-depleted death composes 824D -> ``player_death`` (game-over)."""
    di &= 0xFFFF
    if rw((di + 4) & 0xFFFF) == 0xFFFF:               # [asm 81F3] inactive target
        return {}
    if (rb((di + 5) & 0xFFFF) & 0x20) == 0:           # [asm 81F9] not a boundary target
        return {}
    hit, writes = hitbox_overlap(rb, rw, PLAYER_X, di)   # [asm 8201] 8D7B (always writes [0xA330])
    if not hit:                                       # [asm 8204] jae
        return writes
    al = (4 - rb(SCROLL_PUSH)) & 0xFF                 # [asm 8206]
    r = (rb(SCROLL_PHASE) - al) & 0xFF                # [asm 820C] sub [0x6C05],al
    writes[SCROLL_PHASE] = (0 if (r & 0x80) else r, 1)   # [asm 8210] jns -> clamp to 0
    writes.update(hurt_effect(rb, rw))               # [asm 8217] 824D
    writes[0x4F2D] = (0x2C, 1)                        # [asm 8220]
    writes[0x6BD0] = (0, 1)                           # [asm 8225]
    writes[PLAYER_YVEL] = (0xFF80, 2)                 # [asm 822A]
    writes[0x4F24] = (3, 1)                           # [asm 8230]
    bvx = 0x80 if _s16(rw(PLAYER_X)) >= _s16(rw(CURSOR_X)) else 0xFF80   # [asm 8235-8242] toward the player
    writes[PLAYER_XVEL] = (bvx, 2)                    # [asm 8244]
    writes[0x6BC7] = (0, 1)                           # [asm 8247]
    return writes


# --- 81B4: the whole camera-boundary crush (3 targets vs the player) ---
class _Ov:
    """Read-through overlay so each sub-call sees the prior ones' writes (81B4's [0xA425] hitbox reads
    [0x4F2A], which the earlier 81F3 bounces may have just set). Byte-level shadow; ``writes`` keeps the
    ordered ``{offset: (value, width)}`` contract (re-inserted on overwrite so last-write wins)."""

    _IS_DGROUP_BACKEND = True   # a dgroup-view backend: RngView binds straight onto it

    def __init__(self, rb, rw):
        self._rb = rb
        self._bytes = {}
        self.writes = {}

    def rb(self, o):
        o &= 0xFFFF
        return self._bytes.get(o, self._rb(o)) & 0xFF

    def rw(self, o):
        o &= 0xFFFF
        return self.rb(o) | (self.rb((o + 1) & 0xFFFF) << 8)

    def apply(self, writes):
        for off, v in writes.items():
            if isinstance(off, str):                     # sentinel (SONG_REQUEST) — pass through to the contract
                self.writes[off] = v
                continue
            val, wd = v
            off &= 0xFFFF
            self.writes.pop(off, None)
            self.writes[off] = (val, wd)
            self._bytes[off] = val & 0xFF
            if wd == 2:
                self._bytes[(off + 1) & 0xFFFF] = (val >> 8) & 0xFF

    def wb(self, o, v):
        self.apply({o & 0xFFFF: (v & 0xFF, 1)})

    def ww(self, o, v):
        self.apply({o & 0xFFFF: (v & 0xFFFF, 2)})


CAM_TARGET_A = 0xA421
CAM_TARGET_B = 0xA423
CAM_TARGET_C = 0xA425
CRUSH_GATE = 0x6BE4
CRUSH_SFX_FLAG = 0x27EA


@oracle_link("1030:81B4",
             "the whole camera-boundary crush (gated off when [0x6BE4]!=0): bounce the player off two boundary "
             "targets [0xA421]/[0xA423] (81F3 each), then test the third [0xA425] (8D7B); on that overlap with a "
             "set [0xA330] detail, drive the player downward [0x4F2A]=0xFFC0 (0xFF80 + sfx 3 when [0x27EA]!=0). "
             "Composes the recovered camera_target_bounce + hitbox_overlap over a read-through overlay so the "
             "[0xA425] hitbox sees the earlier bounces' [0x4F2A].",
             "OBSERVED", merge_target="object_spawn")
def camera_boundary_collision(rb, rw):
    """[asm 81B4] Returns the ``{offset: (value, width)}`` writes; the lives-depleted death composes 824D ->
    ``player_death`` (the recovered game-over)."""
    if rb(CRUSH_GATE) != 0:                           # [asm 81B4] crush disabled this frame
        return {}
    ov = _Ov(rb, rw)
    ov.apply(camera_target_bounce(ov.rb, ov.rw, ov.rw(CAM_TARGET_A)))   # [asm 81BE] 81F3
    ov.apply(camera_target_bounce(ov.rb, ov.rw, ov.rw(CAM_TARGET_B)))   # [asm 81C5] 81F3
    hit, hb = hitbox_overlap(ov.rb, ov.rw, PLAYER_X, ov.rw(CAM_TARGET_C))   # [asm 81D0] 8D7B
    ov.apply(hb)
    if hit and hb[0xA330][0] != 0:                    # [asm 81D3 jae / 81D5 cmp [0xA330],0]
        ax = 0xFF80 if ov.rb(CRUSH_SFX_FLAG) != 0 else 0xFFC0   # [asm 81DC-81E6] (+ sfx 3 when set)
        ov.apply({PLAYER_YVEL: (ax, 2)})              # [asm 81EF]
    return ov.writes


# --- 71AB..7534: the camera SEQUENCER state machine (8 states on [0x91FE]) ---
CAM_TIMER = 0xA3F7         # per-state frame timer
SCRIPT_PTR = 0xA401        # the active camera-script pointer (table 0xA427/0xA434/...)


def _cam_scroll_velocity(rb, rw):
    """[asm 7301-7346] ramp the scroll velocity [0x6C08]/[0x6C06] from the cursor-player gap, capped by
    [0x91FB]*0x50; direction is toward the player."""
    bx = (rb(SCROLL_PUSH) * 0x50) & 0xFFFF               # [asm 7301-7308]
    w = {SCRIPT_PTR: (0xA45E, 2)}                         # [asm 730A]
    diff = (rw(CURSOR_X) - rw(PLAYER_X)) & 0xFFFF         # [asm 7310-7313]
    adist = _abs16(diff)                                  # [asm 7318-731A]
    if adist <= bx:                                       # [asm 731C] jbe 7328
        q = (adist // 0xE) & 0xFF                         # [asm 7328-732C] 8-bit div, ah cleared
        v = (q << 4) & 0xFFFF                             # [asm 7330]
        axv = (-v) & 0xFFFF                               # [asm 7336]
        dxv = (v >> 1) & 0xFFFF                           # [asm 7332-7334]
    else:                                                 # [asm 7320-7323]
        axv, dxv = 0xFFA0, 0x30
    w[SCROLL_VY] = (axv, 2)                               # [asm 7338]
    w[SCROLL_VX] = (dxv if _s16(diff) < 0 else (-dxv) & 0xFFFF, 2)   # [asm 733C-7342]
    return w


@oracle_link("1030:94F3",
             "the boss-death diamond burst: spawn 4 sprites at the [0xA336]/[0xA338] origin, each a RANDOM "
             "bonus-popup sprite id from roll_bonus_sprite_id (8C13, advancing the rng), Xvel cycling +/-0x20/"
             "+/-0x10 and Yvel 0xFF60 stepping down 0x10 — via the verified spawn_effect_burst over a read-through "
             "overlay. (Like boss_hit_burst 6BDB, but with a rolled sprite id per spawn.)",
             "OBSERVED", merge_target="object_spawn")
def boss_death_burst_94f3(rb, rw):
    """[asm 94F3..951E] Returns the ``{offset: (value, width)}`` writes (4 spawned 0x50A8 slots + rng state)."""
    ov = _Ov(rb, rw)
    ax, dx = 0x20, 0xFF60                                  # [asm 94F9-94FC]
    for _ in range(4):                                     # [asm 94F6 cx=4]
        sid = roll_bonus_sprite(RngView(ov))               # [asm 9504] 8C13 (advances the rng via the view)
        ov.apply({0xA33A: (sid, 2)})                       # [asm 9507] the burst sprite id
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, ax, dx, 1))   # [asm 950B] 8D1B (one sprite)
        ax = (-ax) & 0xFFFF                                # [asm 950E] neg ax
        if not (ax & 0x8000):                              # [asm 9510] js -> skip the step
            ax = (ax - 0x10) & 0xFFFF                      # [asm 9512]
            dx = (dx - 0x10) & 0xFFFF                      # [asm 9515]
    return ov.writes


@oracle_link("1030:7491",
             "the camera state-6 boss-REACH finale (the gorilla defeat): disable the camera SM ([0x91FE]=0xFF), "
             "clear the 5 boss target render records ([0x564C..0x5694]=0xFFFF), then spawn 4 death-bursts (94F3) "
             "in a diamond around the boss origin [0x5648]/[0x564A] (+0,0 / +0x20,+0x20 / -0x20,+0x20 / -0x40,"
             "-0x20) plus one final sprite (id 0x63, no velocity). Composes boss_death_burst_94f3 + "
             "spawn_effect_burst over a read-through overlay.",
             "OBSERVED", merge_target="object_spawn")
def camera_state6_finale(rb, rw):
    """[asm 7491..74E9] Returns the ``{offset: (value, width)}`` write contract for the boss-reach finale."""
    ov = _Ov(rb, rw)
    ov.apply({0x91FE: (0xFF, 1)})                          # [asm 7491] disable the camera state machine
    for tgt in PlayerGlobals(ov).boss_targets:            # [asm 7496-74A5] clear the 5 target render records
        tgt.sprite = 0xFFFF
    g = PlayerGlobals(ov)
    g.burst_x = g.boss_x                                   # [asm 74A8-74B1] the boss origin
    g.burst_y = g.boss_y
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 74B4] burst 1 at (X, Y)
    g.burst_x = (g.burst_x + 0x20) & 0xFFFF                                 # [asm 74B7-74BC]
    g.burst_y = (g.burst_y + 0x20) & 0xFFFF
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 74C1] burst 2 at (X+0x20, Y+0x20)
    g.burst_x = (g.burst_x - 0x40) & 0xFFFF                                 # [asm 74C4]
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 74C9] burst 3 at (X-0x20, Y+0x20)
    g.burst_x = (g.burst_x - 0x20) & 0xFFFF                                 # [asm 74CC-74D1]
    g.burst_y = (g.burst_y - 0x40) & 0xFFFF
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 74D6] burst 4 at (X-0x40, Y-0x20)
    g.burst_sprite = 0x63                                  # [asm 74E0] final sprite id
    ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0, 0, 1))   # [asm 74D9-74E6] 8D1B (Xvel=0, Yvel=0)
    return ov.writes


@oracle_link("1030:71AB",
             "the camera SEQUENCER: an 8-state machine on [0x91FE] driving the level-intro/boss camera pan. Each "
             "state advances the timer [0xA3F7], gates on the player-cursor distance [0xA3FB] / level param "
             "[0x91FC], sets the active camera-script pointer [0xA401], computes the scroll velocity "
             "[0x6C06]/[0x6C08], and transitions [0x91FE]; composes the recovered inc_scroll_phase (757A) + "
             "camera_boundary_collision (81B4). State 6 is the boss-reach FINALE (camera_state6_finale: disable + "
             "94F3 x4 + burst, RETs directly at 74E9).",
             "OBSERVED", merge_target="object_spawn")
def camera_state_machine(rb, rw):
    """[asm 71AB..7534] Returns the ``{offset: (value, width)}`` writes at the 7534 exit (or the state-6 finale's
    writes, which the ASM RETs at 74E9)."""
    state = rb(CAM_STATE)
    dist = rw(DIST_X)
    g0 = PlayerGlobals(DictBackend(rb, rw))               # read-only named access
    w = {}

    if state == 0:                                        # [asm 71AB]
        if dist >= 0xFA:                                  # [asm 71B2] jb
            w[SCROLL_PHASE] = (0, 1)                       # [asm 71BA]
            w[SCRIPT_PTR] = (0xA427, 2)                    # [asm 71BF]
        else:
            w[CAM_TIMER] = (0, 2)                          # [asm 71C8]
            w[CAM_STATE] = (1, 1)                          # [asm 71CE]
        return w

    if state == 1:                                        # [asm 71D6]
        if dist >= 0xFA:                                  # [asm 71E0] jb 71F0
            w[CAM_STATE] = (0, 1)                          # [asm 71E8]
            return w
        t = (rw(CAM_TIMER) + 1) & 0xFFFF                   # [asm 71F0]
        w[CAM_TIMER] = (t, 2)
        if rw(SPAWN_COUNT) < 0x3C:                         # [asm 71F4] jae 7203
            w[CAM_STATE] = (2, 1)                          # [asm 71FB]
            return w
        w[SCRIPT_PTR] = (0xA434, 2)                        # [asm 7203]
        if t >= 0x6E:                                      # [asm 7209] jb 7221
            w[CAM_STATE] = (2, 1)                          # [asm 7210]
            w.update(inc_scroll_phase(rb))                # [asm 7215] 757A
            w[CAM_TIMER] = (0, 2)                          # [asm 7218]
            return w
        if g0.anim_gate != 0 and (g0.frame_blink & 7) == 0:   # [asm 7221/7228]
            w.update(inc_scroll_phase(rb))                # [asm 722F] 757A
            return w
        c = rb(SCROLL_PHASE)                               # [asm 7235]
        if c >= 0xA:                                       # jb 724A
            w[CAM_STATE] = (3, 1); w[CAM_TIMER] = (0, 2)    # [asm 723C/7241]
        elif c > 3:                                        # [asm 724A] ja 7254
            w[CAM_STATE] = (2, 1); w[CAM_TIMER] = (0, 2)    # [asm 7254/7259]
        return w

    if state == 2:                                        # [asm 7262]
        t = (rw(CAM_TIMER) + 1) & 0xFFFF                   # [asm 726C]
        w[CAM_TIMER] = (t, 2)
        if t < 0x2C:                                       # [asm 7270/7277]
            w.update(camera_boundary_collision(rb, rw))   # [asm 7279] 81B4
            if dist >= 0x4B:                               # [asm 727C] jae 7290
                w[SCRIPT_PTR] = (0xA43B, 2)                # [asm 7290]
            else:
                w[CAM_STATE] = (3, 1); w[CAM_TIMER] = (0, 2)   # [asm 7283]
            return w
        if t == 0x2C:                                      # [asm 7275] je 7299
            w.update(camera_boundary_collision(rb, rw))   # [asm 7299] 81B4
            w[SCROLL_VY] = (0xFF20, 2)                     # [asm 729C]
            if rw(SPAWN_COUNT) < 0x28 and dist < 0x50:      # [asm 72A2/72A9]
                w[CAM_STATE] = (4, 1); w[CAM_TIMER] = (0, 2)   # [asm 72B0/72B5]
            w[SCRIPT_PTR] = (0xA45E, 2)                    # [asm 72BB]
            return w
        # t > 0x2C  [asm 72C4]
        if _s16(rw(SCROLL_VY)) > 0:                        # [asm 72C4] jle 72DB
            w[SCRIPT_PTR] = (0xA460 if rw(SPAWN_COUNT) >= 0x64 else 0xA462, 2)   # [asm 72CB-72D8]
        if dist > 0x50:                                    # [asm 72DB] ja 72F5
            if t < 0x58:                                   # [asm 72F5] jae 72FF
                return w
            if t == 0x58:                                  # [asm 72FF] jne 7349
                w.update(_cam_scroll_velocity(rb, rw))    # [asm 7301-7346]
                return w
            if rw(SCROLL_VY) == 0:                         # [asm 7349] je 7353
                w[SCROLL_PHASE] = (3, 1); w[CAM_TIMER] = (0, 2); w[CAM_STATE] = (1, 1)   # [asm 7353-735E]
            return w
        w[CAM_TIMER] = (0, 2); w[CAM_STATE] = (3, 1); w[SCROLL_PHASE] = (0xB, 1)   # [asm 72E2-72ED]
        return w

    if state == 3:                                        # [asm 7366]
        w.update(camera_boundary_collision(rb, rw))       # [asm 7370] 81B4
        t = (rw(CAM_TIMER) + 1) & 0xFFFF                   # [asm 7373]
        w[CAM_TIMER] = (t, 2)
        limit = 0x9A if _abs16((rw(SPAWN_COUNT) - 0x32) & 0xFFFF) <= 0x19 else 0x42   # [asm 7377-7389]
        if t > limit:                                      # [asm 738C] ja 740C
            w[CAM_TIMER] = (0, 2); w[CAM_STATE] = (2, 1)    # [asm 740C]
            return w
        vy = _s16(rw(SCROLL_VY))                            # [asm 7392]
        if vy < 0:                                          # [asm 7397 je / 7399 jg]
            w[SCRIPT_PTR] = (0xA45E, 2)                    # [asm 739B]
            return w
        if vy > 0:
            w[SCRIPT_PTR] = (0xA464, 2)                    # [asm 73A4]
            return w
        a = rw(DIST_X)                                      # [asm 73AD] vy == 0
        if a >= 0x64:                                       # [asm 73B0] jae 73F1
            w[SCROLL_VY] = (0xFFAF, 2)                      # [asm 73F1]
            dx = 0x50 if _s16(rw(PLAYER_X)) > _s16(rw(CURSOR_X)) else (-0x50) & 0xFFFF   # [asm 73FA-7403]
            w[SCROLL_VX] = (dx, 2)                          # [asm 7405]
        elif a <= 0x19:                                     # [asm 73B5] jbe 73DA
            w[SCRIPT_PTR] = (0xA470, 2)                    # [asm 73DA]
        elif a <= 0x23:                                     # [asm 73BA] jbe 73E3
            w[CAM_STATE] = (4, 1); w[CAM_TIMER] = (0, 2)    # [asm 73E3]
        else:                                               # [asm 73BF] 0x24..0x63
            sp = w[SCROLL_PHASE][0] if SCROLL_PHASE in w else rb(SCROLL_PHASE)   # 81B4 may have knocked it down
            if sp == 0:                                     # [asm 73BF] jne 73D1
                w[CAM_STATE] = (7, 1); w[CAM_TIMER] = (0, 2)   # [asm 73C6]
            w[SCRIPT_PTR] = (0xA46B, 2)                    # [asm 73D1]
        return w

    if state == 4:                                        # [asm 741A]
        w.update(camera_boundary_collision(rb, rw))       # [asm 7421] 81B4
        t = (rw(CAM_TIMER) + 1) & 0xFFFF                   # [asm 7424]
        w[CAM_TIMER] = (t, 2)
        if t > 0x42:                                       # [asm 7428] jbe 743D
            w[CAM_STATE] = (3, 1); w[CAM_TIMER] = (0, 2)    # [asm 742F]
            return w
        w[SCRIPT_PTR] = (0xA475, 2)                        # [asm 743D]
        if rw(SCROLL_VY) == 0:                             # [asm 7443] jne 744F
            w[0x6BEA] = (4, 1)                             # [asm 744A]
        return w

    if state == 5:                                        # [asm 7452]
        t = (rw(CAM_TIMER) + 1) & 0xFFFF                   # [asm 7459]
        w[CAM_TIMER] = (t, 2)
        if t > 0x13:                                       # [asm 745D] jbe 7471
            w[CAM_TIMER] = (0, 2); w[CAM_STATE] = (1, 1)    # [asm 7464-7469]
        else:
            w[SCRIPT_PTR] = (0xA47E, 2)                    # [asm 7471]
        return w

    if state == 6:                                        # [asm 747A]
        w[SCRIPT_PTR] = (0xA480, 2)                        # [asm 7481]
        if _s16(rw(SCROLL_VY)) < 0:                        # [asm 7487] jge 7491
            return w
        for off, vw in camera_state6_finale(rb, rw).items():   # [asm 7491-74E9] the boss-reach death finale
            w[off] = vw
        return w

    if state == 7:                                        # [asm 74EA]
        t = (rw(CAM_TIMER) + 1) & 0xFFFF                   # [asm 74F1]
        w[CAM_TIMER] = (t, 2)
        if t > 0x2C:                                       # [asm 74F5] jbe 7503
            w[CAM_STATE] = (1, 1)                          # [asm 74FC]
            return w
        w[SCRIPT_PTR] = (0xA45E, 2)                        # [asm 7503]
        vy = rw(SCROLL_VY)
        if vy == 0:                                        # [asm 7509] je 751A
            w[SCROLL_VY] = (0xFF9F, 2)                      # [asm 751A]
            dx = 0xFFD0 if _s16(rw(PLAYER_X)) > _s16(rw(CURSOR_X)) else 0x30   # [asm 7523-752C]
            w[SCROLL_VX] = (dx, 2)                          # [asm 752E]
        elif _s16(vy) > 0:                                 # [asm 7510] jl 7534
            w[SCRIPT_PTR] = (0xA460, 2)                    # [asm 7512]
        return w

    return w                                               # [asm 74EF jne 7534] states >= 8 / 0xFF: no-op


# --- 94DC: the precomputed camera-offset table lookup (used by the 93F6 geometry) ---
CAM_OFFSET_TABLE = 0xA6ED   # {dx-key, ax-key, value} entries, stride 6


@oracle_link("1030:94DC",
             "the precomputed camera-offset lookup: scan the [0xA6ED] table (stride 6: dx-key, ax-key, value) "
             "for the (dx, ax) pair and return the value word. The camera-target geometry is baked into this "
             "table, so there is no runtime trig — 93F6 just looks up each target's offset.",
             "OBSERVED", merge_target="object_spawn")
def camera_offset_lookup(rw, key_dx, key_ax):
    """[asm 94DC] ``rw`` reads a DGROUP word. Returns the looked-up offset word; raises :class:`Pre2SpawnGap`
    if the pair is absent (the ASM would scan off the end of the table)."""
    key_dx &= 0xFFFF
    key_ax &= 0xFFFF
    bx = CAM_OFFSET_TABLE
    for _ in range(4096):                                  # [asm 94E0-94F1] scan to the matching entry
        if rw(bx) == key_dx and rw((bx + 2) & 0xFFFF) == key_ax:
            return rw((bx + 4) & 0xFFFF)                   # [asm 94E9]
        bx = (bx + 6) & 0xFFFF
    raise Pre2SpawnGap("94DC camera-offset (dx, ax) not found in the [0xA6ED] table")


# --- 93F6: the camera-target geometry chain (target 0 off the cursor, 1-3 off target 0) ---
CURSOR_SNAP_X = 0xA403     # the cursor X the tail latched before the script command
CURSOR_SNAP_Y = 0xA405
DIST_DIR_FLAG = 0xA3FA     # player facing -> mirror the geometry in X
CMD_BYTE = 0xA3F9          # the script command byte (bit6 = a vertical nudge)


def _cbw(v):
    """al -> ax sign-extend (capstone prints this opcode as ``cwde``; it is ``cbw``)."""
    v &= 0xFF
    return (v - 0x100) & 0xFFFF if v & 0x80 else v


@oracle_link("1030:93F6",
             "the camera-target GEOMETRY chain: read 5 script param words via lodsw, look up each target's "
             "offset (94DC), and lay out 4 targets — target 0 = cursor [0xA403]/[0xA405] +/- offset, targets "
             "1/2/3 = target 0 +/- offset — mirroring X by player facing [0xA3FA] and nudging Y when the command "
             "[0xA3F9]&0x40 is set. Stores the param words [0xA407]/[0xA40D]/[0xA413]/[0xA419]/[0xA41F] (bit15 "
             "flipped when facing) + the 4 positions [0xA409]..[0xA41D]. Composes camera_offset_lookup.",
             "OBSERVED", merge_target="object_spawn")
def camera_target_geometry(rb, rw, si):
    """[asm 93F6] ``si`` = the script command struct offset. Returns ``(writes, new_si)`` (si advanced past the
    5 consumed param words)."""
    w = {}
    face = rb(DIST_DIR_FLAG) != 0
    flip = lambda p: ((p ^ 0x8000) & 0xFFFF) if face else (p & 0xFFFF)
    cx = rw(CURSOR_SNAP_X)
    cy = rw(CURSOR_SNAP_Y)

    p1 = rw(si); si = (si + 2) & 0xFFFF                   # [asm 93F9] lodsw
    p2 = rw(si); si = (si + 2) & 0xFFFF                   # [asm 93FF] lodsw
    off = camera_offset_lookup(rw, p1, p2)               # [asm 9403] 94DC
    t0x = (cx + (-_cbw(off) if face else _cbw(off))) & 0xFFFF    # [asm 9406-9412]
    t0y = (cy + _cbw(off >> 8)) & 0xFFFF                  # [asm 9419-941C]
    w[0xA407] = (flip(p1), 2); w[0xA40D] = (flip(p2), 2)
    w[0xA409] = (t0x, 2); w[0xA40B] = (t0y, 2)

    for poff, txo, tyo in ((0xA413, 0xA40F, 0xA411), (0xA419, 0xA415, 0xA417), (0xA41F, 0xA41B, 0xA41D)):
        p = rw(si); si = (si + 2) & 0xFFFF               # [asm 9427/944F/9477] lodsw
        off = camera_offset_lookup(rw, p2, p)            # [asm 942B/9453/947B] 94DC (shared first key p2)
        tx = (t0x + (-_cbw(off) if face else _cbw(off))) & 0xFFFF    # relative to target 0
        ty = (t0y + _cbw(off >> 8)) & 0xFFFF
        w[poff] = (flip(p), 2); w[txo] = (tx, 2); w[tyo] = (ty, 2)

    if rb(CMD_BYTE) & 0x40:                               # [asm 949B-94AF] vertical nudge
        w[0xA40B] = ((w[0xA40B][0] + 2) & 0xFFFF, 2)
        w[0xA411] = ((w[0xA411][0] + 1) & 0xFFFF, 2)
        w[0xA417] = ((w[0xA417][0] + 1) & 0xFFFF, 2)
        w[0xA41D] = ((w[0xA41D][0] + 1) & 0xFFFF, 2)
    return w, si


# --- 93B2: populate the 5 camera-target records from the script command ---
TARGET_RECORDS = 0x5648    # 5 records, stride 0x12 (first 3 words copied from the geometry)
TARGET_STRIDE = 0x12


@oracle_link("1030:93B2",
             "build the 5 camera-target records: compute the geometry (93F6), then for each of the command's 5 "
             "source pointers copy 3 words from the geometry into the records @0x5648 (stride 0x12), and set the "
             "collision refs [0xA423]/[0xA421]/[0xA425] = the record offset whose source is "
             "[0xA415]/[0xA41B]/[0xA40F]. Composes camera_target_geometry over a read-through overlay so the "
             "copy reads the geometry it just wrote.",
             "OBSERVED", merge_target="object_spawn")
def camera_script_command(rb, rw, si):
    """[asm 93B2] ``si`` = the command struct offset (cmd*0x14 + 0xA571). Returns ``(writes, new_si)``."""
    ov = _Ov(rb, rw)
    geom, si = camera_target_geometry(ov.rb, ov.rw, si)   # [asm 93B9] 93F6
    ov.apply(geom)
    di = TARGET_RECORDS
    for _ in range(5):                                    # [asm 93BF dx=5]
        bx = ov.rw(si)                                    # [asm 93C2] source pointer
        si = (si + 2) & 0xFFFF                            # [asm 93E2]
        if bx == 0xA415:                                  # [asm 93C4]
            ov.apply({0xA423: (di, 2)})
        if bx == 0xA41B:                                  # [asm 93CE]
            ov.apply({0xA421: (di, 2)})
        if bx == 0xA40F:                                  # [asm 93D8]
            ov.apply({0xA425: (di, 2)})
        ov.apply({di: (ov.rw(bx), 2),                     # [asm 93E4-93EE] copy 3 words
                  (di + 2) & 0xFFFF: (ov.rw((bx + 2) & 0xFFFF), 2),
                  (di + 4) & 0xFFFF: (ov.rw((bx + 4) & 0xFFFF), 2)})
        di = (di + TARGET_STRIDE) & 0xFFFF                # [asm 93EF] +6 (stosw x3) +0xC
    return ov.writes, si


# --- 7534..7579: the camera-script bytecode interpreter (the 70D7 tail) ---
SCRIPT_CURSOR = 0xA3FF     # the live script cursor (points into the 0xA427+ bytecode)
SCRIPT_LAST = 0x6C0A       # the script pointer last seen (reset the cursor when [0xA401] changes)


@oracle_link("1030:7534",
             "the camera-script bytecode interpreter (the 70D7 tail): when the state machine's script pointer "
             "[0xA401] changes, reset the cursor [0xA3FF]; skip JUMP opcodes (negative bytes advance the cursor "
             "by the signed offset); then dispatch the command byte (struct si = (cmd&0xBF)*0x14 + 0xA571), "
             "latch the cursor pos [0xA403]/[0xA405] = [0x91FF]/[0x9201], run the command (93B2), and scan the "
             "camera targets (80DE). Composes the recovered camera_script_command + scan_camera_targets over a "
             "read-through overlay.",
             "OBSERVED", merge_target="object_spawn")
def camera_script_interp(rb, rw):
    """[asm 7534..7579] Returns the ``{offset: (value, width)}`` write contract."""
    ov = _Ov(rb, rw)
    ax = ov.rw(SCRIPT_PTR)                                # [asm 7534]
    if ov.rw(SCRIPT_LAST) != ax:                          # [asm 7537]
        ov.apply({SCRIPT_LAST: (ax, 2), SCRIPT_CURSOR: (ax, 2)})   # [asm 753D-7540] reset the cursor
    for _ in range(4096):                                 # [asm 7543] skip JUMP opcodes
        cursor = ov.rw(SCRIPT_CURSOR)
        al = ov.rb(cursor)
        if not (al & 0x80):                              # [asm 7549] jns -> a command byte
            break
        ov.apply({SCRIPT_CURSOR: ((cursor + _cbw(al)) & 0xFFFF, 2)})   # [asm 754D-754E] jump by the signed offset
    else:
        raise Pre2SpawnGap("7534 camera-script JUMP loop did not terminate")
    ov.apply({CMD_BYTE: (al, 1)})                         # [asm 7554]
    cmd = al & 0xBF                                       # [asm 7557]
    ov.apply({SCRIPT_CURSOR: ((ov.rw(SCRIPT_CURSOR) + 1) & 0xFFFF, 2)})   # [asm 7559]
    si = (cmd * 0x14 + 0xA571) & 0xFFFF                   # [asm 755D-7563]
    ov.apply({CURSOR_SNAP_X: (ov.rw(CURSOR_X), 2), CURSOR_SNAP_Y: (ov.rw(CURSOR_Y), 2)})   # [asm 7567-7570]
    cmd_writes, _ = camera_script_command(ov.rb, ov.rw, si)   # [asm 7573] 93B2
    ov.apply(cmd_writes)
    ov.apply(scan_camera_targets(ov.rb, ov.rw))          # [asm 7576] 80DE
    return ov.writes


# --- 70D7..7579: the whole per-frame level-camera / scroll engine ---
@oracle_link("1030:70D7",
             "the whole per-frame level-camera/scroll engine (called from 6822 when [0x91FE]!=0xFF): advance the "
             "spawn cursor + scroll (tick_scroll_cursor), follow the player (player_cursor_dist), and unless the "
             "player is too far (the cull -> jmp 7579), run the camera sequencer (camera_state_machine) and its "
             "bytecode script (camera_script_interp). Composes all the recovered 70D7 leaves over one "
             "read-through overlay so each stage sees the prior writes. Raises Pre2SpawnGap on the gated state-6 "
             "boss-reach finale (94F3).",
             "VERIFIED", merge_target="object_spawn")
def camera_engine(rb, rw, read_tile):
    """[asm 70D7..7579] ``rb``/``rw`` read DGROUP; ``read_tile`` reads the level tile map. Returns the
    ``{offset: (value, width)}`` write contract for the whole engine."""
    ov = _Ov(rb, rw)
    ov.apply(tick_scroll_cursor(ov.rb, ov.rw, read_tile))   # [asm 70D7..7172] cursor + scroll + spawn gate
    dist_w, cull = player_cursor_dist(ov.rw)                # [asm 7172..71AB] player-follow distance
    ov.apply(dist_w)
    if cull:                                                # [asm 7195/71A8] too far -> jmp 7579
        return ov.writes
    # [asm 747A-748C] the state-6 boss-reach finale (state 6 & SCROLL_VY>=0) RETs at 74E9 -> skips 7534
    finale = ov.rb(CAM_STATE) == 6 and _s16(ov.rw(SCROLL_VY)) >= 0
    ov.apply(camera_state_machine(ov.rb, ov.rw))           # [asm 71AB..7534] the camera sequencer
    if not finale:
        ov.apply(camera_script_interp(ov.rb, ov.rw))       # [asm 7534..7579] its bytecode script
    return ov.writes


# --- 6ADD: the mode-9 (last-boss) spawn driver ---
M9_INIT_FLAG = 0xA517      # -1 until the mode-9 state is seeded
M9_PTR = 0xA4F7
M9_COUNT = 0xA519          # spawn-count seed (>>2 = the effect-row width)


@oracle_link("1030:6ADD",
             "the mode-9 (last-boss) engine HEAD (6ADD..6B0C), dispatched from 6822 when [0x2D8A]==9: on first "
             "entry ([0xA517]==-1) seed the boss state ([0xA4F7]=0xA4F9 script ptr, [0xA519]=0x18 boss health, "
             "[0xA515]=0 dwell, [0xA516]=3 cycle); then spawn the per-frame effect row of [0xA519]>>2 slots "
             "(7585). The continuation 6B1C+ (the boss-script advance + projectile-hit detection + glyph-script "
             "interpreter) is the rest of the engine, recovered separately. Composes init_effect_row; 7585's "
             "sound is an audio seam.",
             "OBSERVED", merge_target="object_spawn")
def tick_mode9_spawn(rb, rw):
    """[asm 6ADD..6B0C] the mode-9 boss-engine head. Returns the ``{offset: (value, width)}`` write contract
    (the seed + the spawn row); the 6B1C+ boss-script engine is a separate routine."""
    writes = {}
    if rw(M9_INIT_FLAG) == 0xFFFF:                     # [asm 6AE7] first frame -> seed the state
        writes[M9_PTR] = (0xA4F9, 2)                   # [asm 6AEE]
        writes[M9_COUNT] = (0x18, 2)                   # [asm 6AF4]
        writes[0xA515] = (0, 1)                        # [asm 6AFA]
        writes[0xA516] = (3, 1)                        # [asm 6AFF]
        count = 0x18
    else:
        count = rw(M9_COUNT)
    writes.update(init_effect_row(_sar16(count, 2) & 0xFFFF))   # [asm 6B04-6B0C] 7585, cx = [0xA519]>>2
    return writes


# --- 6BDB: the boss-hit diamond burst (spawned when a projectile damages the boss) ---
@oracle_link("1030:6BDB",
             "the boss-hit effect burst: spawn 4 sprites (id 0x2137) at the boss origin (0xB9, 0x1E) in a "
             "diamond — Xvel cycles +/-0x20, +/-0x10 and Yvel steps 0xFF60 -> 0xFF50 — via the verified "
             "spawn_effect_burst (one sprite per call), over a read-through overlay so each spawn takes the next "
             "free 0x50A8 slot.",
             "OBSERVED", merge_target="object_spawn")
def boss_hit_burst(rb, rw):
    """[asm 6BDB..6C0C] Returns the ``{offset: (value, width)}`` writes (4 spawned 0x50A8 slots)."""
    ov = _Ov(rb, rw)
    ov.apply({0xA336: (0xB9, 2), 0xA338: (0x1E, 2), 0xA33A: (0x2137, 2)})   # [asm 6BDB-6BF5] origin + sprite
    ax, dx = 0x20, 0xFF60                                  # [asm 6BEA-6BED]
    for _ in range(4):                                     # [asm 6BE7 cx=4]
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, ax, dx, 1))   # [asm 6BFC] 8D1B (one sprite)
        ax = (-ax) & 0xFFFF                                # [asm 6BFF] neg ax
        if not (ax & 0x8000):                              # [asm 6C01] js -> skip the step
            ax = (ax - 0x10) & 0xFFFF                      # [asm 6C03]
            dx = (dx - 0x10) & 0xFFFF                      # [asm 6C06]
    return ov.writes


# --- 6CA7 / 6CF1: the boss-attack projectile spawns (opcodes 0xFF / 0xFE of the boss script) ---
BOSS_POOL_LO = 0x50A8      # the shared 0x50A8 effect/projectile pool (0x20 slots, stride 0x12)
BOSS_POOL_N = 0x20
BOSS_POOL_STRIDE = 0x12
RNG_LCG_LO = 0x2CEC        # the 4-byte rng_lcg state a/b/c (bytes) + d (word at 0x2CEF)


def _free_boss_slot(rw):
    """[asm 6CA9-6CB8] the first free 0x50A8 slot ([+4]==0xFFFF), or None when the pool is full."""
    si = BOSS_POOL_LO
    for _ in range(BOSS_POOL_N):
        if rw((si + 4) & 0xFFFF) == 0xFFFF:
            return si
        si = (si + BOSS_POOL_STRIDE) & 0xFFFF
    return None


def _rng_lcg_next(rb, rw, writes):
    """[asm 39DF via call] advance rng_lcg over [0x2CEC..0x2CEF], record the state writeback, return AL=b'."""
    a, b, c, d, ret = rng_lcg(rb(RNG_LCG_LO), rb(RNG_LCG_LO + 1), rb(RNG_LCG_LO + 2), rw(RNG_LCG_LO + 3))
    writes[RNG_LCG_LO] = (a, 1)
    writes[RNG_LCG_LO + 1] = (b, 1)
    writes[RNG_LCG_LO + 2] = (c, 1)
    writes[RNG_LCG_LO + 3] = (d, 2)
    return ret


@oracle_link("1030:6CA7",
             "boss-script opcode 0xFF: spawn a boss projectile (sprite 0x1CA) into the first free 0x50A8 slot at "
             "(0xC8, 0x58), anim [+0xC]=0x84, with a random upward-left Xvel = -((rng_lcg & 0xF) << 3). No-op "
             "when the pool is full. Composes the recovered rng_lcg.",
             "OBSERVED", merge_target="object_spawn")
def spawn_boss_bolt_1ca(rb, rw):
    """[asm 6CA7..6CF0] Returns the ``{offset: (value, width)}`` writes (the spawned slot + rng state)."""
    si = _free_boss_slot(rw)
    if si is None:                                         # [asm 6CBA] pool full -> no spawn
        return {}
    writes: dict = {}
    bolt = EffectParticle(WidthContractBackend(rb, rw, writes), si)
    bolt.sprite = 0x1CA                                    # [asm 6CBC]
    bolt.x = 0xC8                                          # [asm 6CC0]
    bolt.y = 0x58                                          # [asm 6CC5]
    bolt.substate = 0                                      # [asm 6CCA]
    bolt.lifetime = 0x84                                   # [asm 6CCE] the anim selector
    bolt.source = 0xFFFF                                   # [asm 6CD3] no source entity
    r = _rng_lcg_next(rb, rw, writes)                     # [asm 6CD8] 39DF
    bolt.xvel = (-(((r & 0xF) << 3) & 0xFFFF)) & 0xFFFF   # [asm 6CDB-6CE6] random upward-left
    bolt.yvel = 0                                          # [asm 6CE9]
    return writes


@oracle_link("1030:6CF1",
             "boss-script opcode 0xFE: spawn a boss projectile (sprite 0x1CB) into the first free 0x50A8 slot at "
             "(random X = (rng_lcg & 0x7F) - 0x10, Y=0), anim [+0xC]=0x42, no velocity. No-op when the pool is "
             "full. Composes the recovered rng_lcg.",
             "OBSERVED", merge_target="object_spawn")
def spawn_boss_bolt_1cb(rb, rw):
    """[asm 6CF1..6D33] Returns the ``{offset: (value, width)}`` writes (the spawned slot + rng state)."""
    si = _free_boss_slot(rw)
    if si is None:                                         # [asm 6D04] pool full -> no spawn
        return {}
    writes: dict = {}
    bolt = EffectParticle(WidthContractBackend(rb, rw, writes), si)
    bolt.sprite = 0x1CB                                    # [asm 6D06]
    r = _rng_lcg_next(rb, rw, writes)                     # [asm 6D0B] 39DF
    bolt.x = ((r & 0x7F) - 0x10) & 0xFFFF                 # [asm 6D0E-6D14] random X
    bolt.y = 0                                             # [asm 6D16]
    bolt.substate = 0                                      # [asm 6D1B]
    bolt.lifetime = 0x42                                   # [asm 6D1F] the anim selector
    bolt.source = 0xFFFF                                   # [asm 6D24] no source entity
    bolt.xvel = 0                                          # [asm 6D2B]
    bolt.yvel = 0                                          # [asm 6D2E]
    return writes


# --- 6B91: the boss glyph-script interpreter ---
BOSS_SCRIPT_PTR = 0xA517   # the live boss-script cursor
BOSS_DWELL = 0xA515        # the dwell counter (decremented by jump opcodes; the 6B1C advance fires at 0)


@oracle_link("1030:6B91",
             "the boss glyph-script interpreter: from the cursor [0xA517], process control opcodes — 0xFF spawn "
             "0x1CA (6CA7), 0xFE spawn 0x1CB (6CF1), 0xFD play sfx 2, a negative byte = a relative jump that also "
             "decrements the dwell [0xA515] (saturating) — until a glyph byte (al>=0): advance [0xA517] by 1, "
             "render it (6C0D, es=0xA000), and return. Composes the recovered boss-bolt spawns; 6C0D + sfx are "
             "render/audio seams.",
             "OBSERVED", merge_target="object_spawn")
def boss_script_interp(rb, rw):
    """[asm 6B91..6BDA] Returns the ``{offset: (value, width)}`` write contract (spawns + cursor + dwell + rng).
    The 6C0D glyph blit and the sfx are seams (not in the contract)."""
    ov = _Ov(rb, rw)
    bx = ov.rw(BOSS_SCRIPT_PTR)                            # [asm 6B91]
    for _ in range(4096):
        al = ov.rb(bx)                                    # [asm 6B95]
        if al == 0xFF:                                    # [asm 6B97] spawn 0x1CA
            ov.apply(spawn_boss_bolt_1ca(ov.rb, ov.rw))
            bx = (bx + 1) & 0xFFFF
        elif al == 0xFE:                                  # [asm 6BA1] spawn 0x1CB
            ov.apply(spawn_boss_bolt_1cb(ov.rb, ov.rw))
            bx = (bx + 1) & 0xFFFF
        elif al == 0xFD:                                  # [asm 6BAB] play sfx 2 (audio seam)
            bx = (bx + 1) & 0xFFFF
        elif al & 0x80:                                   # [asm 6BB8] negative -> relative jump + dwell
            d = ov.rb(BOSS_DWELL)                         # [asm 6BC0-6BC5] sub 1 ; adc 0 (saturate at 0)
            ov.apply({BOSS_DWELL: (((d - 1) & 0xFF) if d else 0, 1)})
            bx = (bx + _cbw(al)) & 0xFFFF                 # [asm 6BCB] bx += signed al
            ov.apply({BOSS_SCRIPT_PTR: (bx, 2)})          # [asm 6BCD]
        else:                                             # [asm 6BD3] glyph -> advance, render (6C0D), return
            ov.apply({BOSS_SCRIPT_PTR: ((ov.rw(BOSS_SCRIPT_PTR) + 1) & 0xFFFF, 2)})
            ov.writes[GLYPH_LATCH] = al                   # [asm 6BD7] the glyph AL handed to the 6C0D render
            break
    else:
        raise Pre2SpawnGap("6B91 boss-script interpreter ran 4096 opcodes without reaching a glyph")
    return ov.writes


# --- 6B1C..6B90: the boss script-advance + projectile-hit detection ---
BOSS_CYCLE = 0xA516        # hit cadence counter (&3); every 4th hit switches to the 0xA540 script
BOSS_PROJ_LO = 0x4F2E      # the 4 player-projectile slots (stride 0x12)
BOSS_ZONE_X = (0xB9, 0x32)  # a projectile hits the boss when X-0xB9 < 0x32 and Y-0x1E < 0x32
BOSS_ZONE_Y = (0x1E, 0x32)


@oracle_link("1030:6B1C",
             "the boss script-advance + projectile-hit detection (between the 6ADD head and the 6B91 glyph "
             "interpreter): when the dwell [0xA515] hits 0, load the next attack-script entry from the table "
             "[0xA4F7] (0xFFFF = a relative wrap) into [0xA517]/[0xA515]; then scan the 4 projectile slots "
             "[0x4F2E] and, on the first one inside the boss zone (X in [0xB9,0xEB), Y in [0x1E,0x50)), switch to "
             "the hit script [0xA517]=0xA534, drop the boss health [0xA519] (saturating; ->6BDB death-burst at "
             "0), and cycle [0xA516]&3 (every 4th -> [0xA517]=0xA540).",
             "OBSERVED", merge_target="object_spawn")
def boss_pre_interp(rb, rw):
    """[asm 6B1C..6B90] Returns the ``{offset: (value, width)}`` write contract. On the killing hit (boss health
    depleted to 0) it also spawns the 6BDB diamond death-burst (the verified boss_hit_burst)."""
    ov = _Ov(rb, rw)
    if ov.rb(BOSS_DWELL) == 0:                            # [asm 6B1C] dwell expired -> next script entry
        bx = ov.rw(M9_PTR)                                # [asm 6B23]
        if ov.rw(bx) == 0xFFFF:                           # [asm 6B29] wrap marker
            bx = (bx + ov.rw((bx + 2) & 0xFFFF)) & 0xFFFF  # [asm 6B2E]
        ov.apply({BOSS_SCRIPT_PTR: (ov.rw(bx), 2),        # [asm 6B33]
                  BOSS_DWELL: (ov.rb((bx + 2) & 0xFFFF), 1),    # [asm 6B36-6B39]
                  M9_PTR: ((bx + 4) & 0xFFFF, 2)})        # [asm 6B3C-6B3F]
    si = BOSS_PROJ_LO                                     # [asm 6B43]
    for _ in range(4):                                    # [asm 6B46] cx=4 slots
        if (ov.rw((si + 4) & 0xFFFF) != 0xFFFF                              # [asm 6B49] active
                and ((ov.rw(si) - BOSS_ZONE_X[0]) & 0xFFFF) < BOSS_ZONE_X[1]    # [asm 6B4F-6B57] X in zone
                and ((ov.rw((si + 2) & 0xFFFF) - BOSS_ZONE_Y[0]) & 0xFFFF) < BOSS_ZONE_Y[1]):  # [asm 6B59-6B62]
            ov.apply({BOSS_SCRIPT_PTR: (0xA534, 2)})      # [asm 6B64] switch to the hit script
            h = ov.rb(M9_COUNT)                           # [asm 6B6A-6B6F] boss health-- (saturating)
            nh = ((h - 1) & 0xFF) if h else 0
            ov.apply({M9_COUNT: (nh, 1)})
            if nh == 0:                                   # [asm 6B74] health depleted -> 6BDB death-burst
                ov.apply(boss_hit_burst(ov.rb, ov.rw))    # [asm 6B76] the diamond burst; then fall through to 6B79
            c = (ov.rb(BOSS_CYCLE) + 1) & 3               # [asm 6B79-6B7D] (runs on both the hit and kill paths)
            ov.apply({BOSS_CYCLE: (c, 1)})
            if c == 0:                                    # [asm 6B82]
                ov.apply({BOSS_SCRIPT_PTR: (0xA540, 2)})  # [asm 6B84]
            break                                         # [asm 6B8A] -> the glyph interpreter
        si = (si + 0x12) & 0xFFFF                         # [asm 6B8C]
    return ov.writes


# --- 6ADD..6BDA: the whole mode-9 last-boss engine spawn path ---
@oracle_link("1030:6ADD",
             "the whole mode-9 last-boss engine (6ADD..6BDA): the head (seed + per-frame effect row) -> while "
             "the boss is alive ([0xA519]!=0) the script-advance + projectile-hit detection (boss_pre_interp) -> "
             "the glyph-script interpreter (boss_script_interp), all over one read-through overlay so each stage "
             "sees the prior writes. On the killing hit it spawns the 6BDB death-burst; once the boss is dead "
             "([0xA519]==0) the head runs and the 6C0D victory-glyph render (a pure page blit, no DGROUP writes) is "
             "skipped as a render seam so the contract ends there.",
             "VERIFIED", merge_target="object_spawn")
def tick_mode9_boss(rb, rw):
    """[asm 6ADD..6BDA] Returns the ``{offset: (value, width)}`` write contract for the whole boss engine."""
    ov = _Ov(rb, rw)
    ov.apply(tick_mode9_spawn(ov.rb, ov.rw))             # [asm 6ADD..6B0C] head (seed + spawn row)
    if ov.rw(M9_COUNT) == 0:                             # [asm 6B0F] boss dead -> 6C0D victory-glyph render (a
        return ov.writes                                 # pure page blit, es:[di], NO DGROUP writes) then RET; the
        #                                                  glyph render is native's own renderer's job, not a
        #                                                  gameplay-state write — so the contract ends at the head.
    ov.apply(boss_pre_interp(ov.rb, ov.rw))             # [asm 6B1C..6B90] script-advance + hit-detection
    ov.apply(boss_script_interp(ov.rb, ov.rw))          # [asm 6B91..6BDA] glyph-script interpreter
    return ov.writes


# --- 6D34..70D6: the level-6 (inside-a-tree) camera/boss state machine ---
#
# Called by the object system (6822) only on level 6 ([0x2D8A]==5, gated on [0x8166]&0xFE==0 & [0xA326]!=3).
# The boss is a set of camera-target records driven by a small state machine keyed on [0xA324..0xA32C]:
#   [0xA324] hit-stun countdown | [0xA325] hits-per-phase (7) | [0xA326] PHASE 0/1/2 (3 = defeated, gated off) |
#   [0xA328] main-target anim index | [0xA329]/[0xA32A] sub-indices | [0xA32B] spawn timer (saturating-dec each
#   frame; fires the projectile-drop + phase logic at 0) | [0xA32C] re-seed countdown for [0xA32B].
# It rewrites the effect row (7585), moves up to 5 falling projectiles (0x7DAF), bakes the target geometry from
# three tables (0xA4B5/0xA485/0xA4E5 -> the render/target records 0x5648..0x5694), runs player<->boss collision,
# and advances the phase when the player's club/projectiles hit a target. The boss-DEATH finale (94F3 x4 + burst
# at phase 3) is _l6_tree_death_finale, recovered + byte-exact (as is the 70D7 state-6 finale camera_state6_finale).
L6_PROJ_LIST = 0x7DAF        # 5 boss projectiles, stride 0xB
L6_PROJ_RENDER = 0x55EE      # their render slots, stride 0x12
L6_STUN = 0xA324
L6_HITS = 0xA325
L6_PHASE = 0xA326
L6_ANIM = 0xA328
L6_SUB_B = 0xA329
L6_SUB_A = 0xA32A
L6_TIMER = 0xA32B
L6_RESEED = 0xA32C
RNG_A, RNG_B, RNG_C, RNG_D = 0x2CEC, 0x2CED, 0x2CEE, 0x2CEF   # rng_lcg (39DF) state


def _sar16(v, n):
    """Arithmetic right shift of a 16-bit value (sign-preserving), returned as a 16-bit word."""
    return (_s16(v) >> n) & 0xFFFF


def _l6_draw_rng(ov):
    """[asm 39DF] advance the LCG over the overlay (writing it back); return the AL byte. (= RngView.roll)"""
    return RngView(ov).roll() & 0xFF


def _l6_spawn_state_machine(ov):
    """[asm 6E92..6F37] the [0xA32B]-timer state machine (reached only when [0xA324]==0 and [0xA32B]==0):
    advance the sub-indices, drop a random falling projectile (unless the new [0xA32A]==1), reload [0xA32B],
    and re-seed [0xA32B]/[0xA32C] from the rng when [0xA32C] underflows."""
    ov.wb(L6_SUB_B, (ov.rb(L6_SUB_B) + 1) % 3)              # [asm 6E92]
    al = (ov.rb(L6_SUB_A) + 1) % 3                          # [asm 6EA0]
    ov.wb(L6_SUB_A, al)
    ah = 1
    if al != 1:                                            # [asm 6EB2 je 6F0F] (skip the spawn)
        ah = 3
        g = PlayerGlobals(ov); p = PlayerView(ov)
        g.camera_shake = 4                                 # [asm 6EB6]
        p.x = (p.x + 2) & 0xFFFF                           # [asm 6EBB] nudge the player X
        slot = next((s for s in g.l6_projectiles if s.free), None)   # [asm 6EC1-6ED0] a free projectile slot
        if slot is not None:                               # [asm 6ED4]
            slot.sprite = 0x1B3                            # [asm 6ED4] sprite id
            slot.y = (p.y - 0x96) & 0xFFFF                 # [asm 6ED9] Y = playerY - 0x96
            ret = _l6_draw_rng(ov)                         # [asm 6EE2] 39DF
            al2 = ret & 0x7C                               # [asm 6EE7] and al,0x7c
            if al2 & 4:                                    # [asm 6EE9 test/6EED neg] randomize the X sign
                al2 = (-al2) & 0xFF
            slot.x = (_cbw(al2) + p.x) & 0xFFFF            # [asm 6EEF-6EF4] X = playerX +/- offset
            slot.fall_vel = (((ret & 3) + 1) << 4) & 0xFFFF   # [asm 6EF6-6F02] fall speed
            slot.drift_acc = 0                             # [asm 6F05]
            slot.drift_step = 4                            # [asm 6F0A]
    ov.wb(L6_TIMER, ah)                                    # [asm 6F0F]
    reseed = (ov.rb(L6_RESEED) - 1) & 0xFF                 # [asm 6F13] dec [0xA32C]
    ov.wb(L6_RESEED, reseed)
    if reseed == 0:                                        # [asm 6F17 jne 6F3C]
        ov.wb(L6_RESEED, ((_l6_draw_rng(ov) & 0xF) << 3) & 0xFF)          # [asm 6F19-6F24]
        ov.wb(L6_TIMER, (((_l6_draw_rng(ov) & 0xF) << 3) + 0x40) & 0xFF)  # [asm 6F27-6F34]
        ov.wb(L6_SUB_B, 0)                                 # [asm 6F37]


def _l6_tail_6f8f(ov):
    """[asm 6F8F..6FBA] the wait-path player collision: on overlap with target 0x5690 (and [0xA330] set) push the
    player down. Returns True if it branched straight to the [0xA32B]-dec (i.e. skip the boss-hit + boundary)."""
    p, g = PlayerView(ov), PlayerGlobals(ov)
    if g.respawn_state != 0:                               # [asm 6F94 je / 6F96 jmp 70CB]
        return True
    hit, hb = hitbox_overlap(ov.rb, ov.rw, 0x4F1C, 0x5690)   # [asm 6F9F] 8D7B
    ov.apply(hb)
    if hit and g.hit_flag != 0:                            # [asm 6FA2 jae / 6FA4 je 6FBB]
        p.yvel = 0xFF80 if g.in_up != 0 else 0xFFC0        # [asm 6FAB-6FB8]
    return False


def _l6_bounce_6f59(ov):
    """[asm 6F59..6F8D] the player bounced off a boss target: pop up ([0x4F2A]=0xFF60); above Y 0x7BB it is a
    full crush (knockback + 3x the 824D hurt effect)."""
    p, g = PlayerView(ov), PlayerGlobals(ov)
    if _s16(p.y) > 0x7BB:                                  # [asm 6F5F jg 6F69]
        p.yvel = 0xFF60                                    # [asm 6F69] full crush: knockback
        p.death_state = 0x2C                               # [asm 6F6E]
        g.anim_gate = 0                                    # [asm 6F74]
        p.motion_mode = 3                                  # [asm 6F79]
        p.xvel = 0xFF80                                    # [asm 6F7E]
        for _ in range(3):                                # [asm 6F84-6F8A] 824D x3
            ov.apply(hurt_effect(ov.rb, ov.rw))
    else:
        p.yvel = 0xFF60                                    # [asm 6F61]


def _l6_tree_death_finale(rb, rw):
    """[asm 7041..70A4] The level-6 tree phase-3 boss-death finale: 4 death-bursts (94F3) in a diamond around the
    boss origin ([0x5648]-0xC8, [0x564A]) + a final sprite (0x63, no velocity), then clear the 5 L6 projectile
    render slots [0x55EE] and the 5 boss target records [0x5648]. Returns the {offset:(value,width)} writes."""
    ov = _Ov(rb, rw)
    g = PlayerGlobals(ov)
    g.burst_x = (g.boss_x - 0xC8) & 0xFFFF                # [asm 7041-7047] boss origin (camera-relative)
    g.burst_y = g.boss_y                                  # [asm 704A-704D]
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 7050] burst 1
    g.burst_x = (g.burst_x + 0x20) & 0xFFFF               # [asm 7053]
    g.burst_y = (g.burst_y + 0x20) & 0xFFFF               # [asm 7058]
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 705D] burst 2
    g.burst_x = (g.burst_x - 0x40) & 0xFFFF               # [asm 7060]
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 7065] burst 3
    g.burst_x = (g.burst_x - 0x20) & 0xFFFF               # [asm 7068]
    g.burst_y = (g.burst_y - 0x40) & 0xFFFF               # [asm 706D]
    ov.apply(boss_death_burst_94f3(ov.rb, ov.rw))         # [asm 7072] burst 4
    g.burst_sprite = 0x63                                 # [asm 707C]
    ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0, 0, 1))  # [asm 7075-7082] final sprite (no velocity)
    for rslot in g.l6_render_slots:                       # [asm 7085-7093] clear the 5 L6 projectile render slots
        rslot.sprite = 0xFFFF
    for tgt in g.boss_targets:                            # [asm 7095-70A3] clear the 5 L6 boss target records
        tgt.sprite = 0xFFFF
    return ov.writes


def _l6_boss_hit(ov):
    """[asm 6FBB..70A4] test the player's club (0x4F0A) then the 4 thrown projectiles (0x4F2E) against the active
    target record set (0x5648 + 0x24*[0xA326]); on a hit, kill the attacker, flash the target, and (every 7 hits)
    advance the phase [0xA326]. Reaching phase 3 runs the boss-death finale (94F3 x4 + burst)."""
    p, g = PlayerView(ov), PlayerGlobals(ov)
    tgt = g.boss_targets[2 * ov.rb(L6_PHASE)]              # [asm 6FBB-6FC7] 0x5648 + 0x24*phase (two records/phase)
    si = tgt.offset
    hit_slot = None
    if p.slot0.sprite != 0xFFFF:                           # [asm 6FC9] the player club (0x4F0A+4)
        g.hit_pass_full = 1                                # [asm 6FCF] full-tolerance hitbox
        h, hb = hitbox_overlap(ov.rb, ov.rw, si, 0x4F0A)   # [asm 6FD4] 8D7B
        ov.apply(hb); g.hit_pass_full = 0                  # [asm 6FD7]
        if h:                                             # [asm 6FDC jb 7001]
            hit_slot = 0x4F0A
    if hit_slot is None:
        for slot in g.projectiles:                        # [asm 6FDE-6FFC] the 4 projectiles
            if not slot.free:                             # [asm 6FE4] active slot
                g.hit_pass_full = 1
                h, hb = hitbox_overlap(ov.rb, ov.rw, si, slot.offset)   # [asm 6FEF]
                ov.apply(hb); g.hit_pass_full = 0
                if h:                                     # [asm 6FF7 jb 7001]
                    hit_slot = slot.offset
                    break
    if hit_slot is None:                                  # [asm 6FFE jmp 70A5]
        return
    tgt.flags = tgt.flags ^ 0x40                          # [asm 7001] flash the target
    if ov.rw(L6_PHASE) != 0:                              # [asm 7005 je 7010]
        prev = g.boss_targets[2 * ov.rb(L6_PHASE) - 1]     # [asm 700C] (si-0xD = the previous record's flags)
        prev.flags = prev.flags ^ 0x40
    RenderSlot(ov, hit_slot).sprite = 0xFFFF              # [asm 7010] kill the attacker
    ov.wb(L6_STUN, 6)                                     # [asm 7015] hit-stun
    if ov.rw(L6_PHASE) < 2:                               # [asm 701A jae 702B]
        ov.wb(L6_SUB_B, 3); ov.wb(L6_SUB_A, 3)            # [asm 7021-7026]
    hits = (ov.rb(L6_HITS) - 1) & 0xFF                    # [asm 702B] dec [0xA325]
    ov.wb(L6_HITS, hits)
    if hits != 0:                                         # [asm 702F jne 70A5]
        return
    ov.wb(L6_HITS, 7)                                     # [asm 7031]
    ov.ww(L6_PHASE, (ov.rw(L6_PHASE) + 1) & 0xFFFF)       # [asm 7036] advance the phase
    if ov.rw(L6_PHASE) != 3:                              # [asm 703A jne 70A5]
        return
    ov.apply(_l6_tree_death_finale(ov.rb, ov.rw))         # [asm 7041-70A4] the phase-3 boss-death finale


def _l6_finish(ov):
    """[asm 70CB..70D6] saturating decrement of the spawn timer [0xA32B] (the `sub`/`adc` idiom)."""
    v = ov.rb(L6_TIMER)
    ov.wb(L6_TIMER, (v - 1) & 0xFF if v != 0 else 0)
    return ov.writes


@oracle_link("1030:6D34",
             "the whole level-6 (inside-a-tree) camera/boss state machine: rewrite the effect row (7585), move "
             "the 5 falling projectiles (0x7DAF -> render 0x55EE) with player collision (8D7B + 824D hurt), bake "
             "the target geometry from the 0xA4B5/0xA485/0xA4E5 tables into the target records (0x5648..0x5694), "
             "run the [0xA32B]-timer spawn state machine (drop a random projectile via 39DF), the player<->boss "
             "collision tails, and the club/projectile-vs-boss hit + phase advance ([0xA326]++ every 7 hits). "
             "Saturating-decrements [0xA32B] each frame. Composes the recovered init_effect_row/hitbox_overlap/"
             "hurt_effect/rng_lcg over a read-through overlay; fails loud on the phase-3 boss-death finale (94F3).",
             "OBSERVED", merge_target="object_spawn")
def tick_level6_boss(rb, rw):
    """[asm 6D34..70D6] Returns the ``{offset: (value, width)}`` write contract for the whole routine. Raises
    :class:`Pre2SpawnGap` on the (unwitnessed) boss-death finale and the lives-depleted 824D death."""
    ov = _Ov(rb, rw)
    p, g = PlayerView(ov), PlayerGlobals(ov)

    ov.apply(init_effect_row(((2 - ov.rw(L6_PHASE)) << 1) & 0xFFFF))   # [asm 6D34-6D3D] 7585 effect row

    # [asm 6D40..6DDD] the 5 falling projectiles: move + player collision, projected to the render slots
    for proj, rslot in zip(g.l6_projectiles, g.l6_render_slots):
        bp = proj.sprite
        if bp != 0xFFFF:                                  # [asm 6D4F] active slot
            killed = False
            if g.respawn_state == 0:                      # [asm 6D54] not the death freeze
                hit, hb = hitbox_overlap(ov.rb, ov.rw, 0x4F1C, rslot.offset)   # [asm 6D5B] 8D7B(player, slot)
                ov.apply(hb)
                if hit:                                   # [asm 6D5E jb -> knock the player back]
                    p.death_state = 0x2C; g.anim_gate = 0                 # [asm 6D60-6D65]
                    p.yvel = 0xFF80; p.motion_mode = 3; p.xvel = 0xFF80   # [asm 6D6A-6D75]
                    ov.apply(hurt_effect(ov.rb, ov.rw))   # [asm 6D7B] 824D
                    bp = 0xFFFF
                    proj.sprite = bp                      # [asm 6DC9-6DCC] kill source
                    rslot.sprite = bp                     # [asm 6DCF]
                    killed = True
            if not killed:
                # [asm 6D80] integrate: Y by fall_vel>>4, X by the oscillating drift accumulator
                proj.y = (proj.y + _sar16(proj.fall_vel, 4)) & 0xFFFF
                proj.drift_acc = (proj.drift_acc + _cbw(proj.drift_step)) & 0xFFFF
                d8 = _s16(proj.drift_acc)                 # [asm 6D95-6DA2] reverse at the +/-0x20 extents
                if d8 >= 0x20 or d8 < -0x20:
                    proj.drift_step = (-proj.drift_step) & 0xFF
                dx4 = _sar16(proj.drift_acc, 4)           # [asm 6DA5]
                if dx4 != 0:                              # [asm 6DAD je 6DB7]
                    bp = (bp + (1 if _s16(dx4) > 0 else 2)) & 0xFFFF   # [asm 6DAF-6DB5] anim by drift sign
                proj.x = (proj.x + dx4) & 0xFFFF          # [asm 6DB7] X += drift
                rslot.x = proj.x                          # [asm 6DB9-6DBB] project X
                y = proj.y
                rslot.y = y                               # [asm 6DBD-6DC0] project Y
                if _s16(y) > 0x7D8:                       # [asm 6DC3 jle keeps]
                    bp = 0xFFFF
                    proj.sprite = bp                      # [asm 6DC9-6DCC] kill off the bottom
                rslot.sprite = bp                         # [asm 6DCF]

    # [asm 6DDE..6E57] bake the target geometry from the three tables into the boss target records
    t0, t1, t2, t3, t4 = g.boss_targets                   # records 0x5648/565A/566C/567E/5690
    t2.sprite = 0xFFFF; t1.sprite = 0xFFFF                # [asm 6DDE-6DE4]
    if ov.rw(L6_PHASE) < 2:                               # [asm 6DE7 jae 6E12]
        si = (0xC * ov.rb(L6_SUB_B) + 0xA4B5) & 0xFFFF     # [asm 6DEE-6DF6]
        t2.x = ov.rw(si); t2.y = ov.rw((si + 2) & 0xFFFF); t2.sprite = ov.rw((si + 4) & 0xFFFF)   # [6DFA-6E02]
        t1.x = ov.rw((si + 6) & 0xFFFF); t1.y = ov.rw((si + 8) & 0xFFFF)                          # [6E05-6E0A]
        t1.sprite = ov.rw((si + 0xA) & 0xFFFF)                                                    # [6E0F]
    si = (0xC * ov.rb(L6_SUB_A) + 0xA485) & 0xFFFF         # [asm 6E12-6E18]
    t4.x = ov.rw(si); t4.y = ov.rw((si + 2) & 0xFFFF); t4.sprite = ov.rw((si + 4) & 0xFFFF)       # [6E1E-6E26]
    t3.x = ov.rw((si + 6) & 0xFFFF); t3.y = ov.rw((si + 8) & 0xFFFF)                              # [6E29-6E2E]
    t3.sprite = ov.rw((si + 0xA) & 0xFFFF)                                                        # [6E33]
    si = (6 * ov.rb(L6_ANIM) + 0xA4E5) & 0xFFFF            # [asm 6E36-6E3C] the main target record
    t0.x = ov.rw(si); t0.y = ov.rw((si + 2) & 0xFFFF)     # [asm 6E42-6E47] (= boss_x / boss_y)
    t0.sprite = 0xFFFF if ov.rw(L6_PHASE) != 0 else ov.rw((si + 4) & 0xFFFF)   # [asm 6E4A-6E55]

    if (g.frame_blink & 3) == 0:                          # [asm 6E58] cycle the anim index every 4th frame
        ov.wb(L6_ANIM, (ov.rb(L6_ANIM) + 1) % 3)          # [asm 6E5F-6E6A]

    # [asm 6E6D..] hit-stun countdown vs the spawn state machine, then the shared collision + boss-hit tails
    stun = ov.rb(L6_STUN)
    run_boss_hit = True
    if stun != 0:                                         # [asm 6E6D-6E88]
        if stun == 1:                                     # [asm 6E74-6E7F]
            ov.wb(L6_SUB_B, 2); ov.wb(L6_SUB_A, (ov.rb(L6_SUB_A) - 1) & 0xFF)
            ov.wb(L6_TIMER, (ov.rb(L6_TIMER) + 5) & 0xFF)
        ov.wb(L6_STUN, (stun - 1) & 0xFF)                 # [asm 6E84]
        run_boss_hit = not _l6_tail_6f8f(ov)              # [asm 6E88 jmp 6F8F]
    elif ov.rb(L6_TIMER) != 0:                            # [asm 6E8B-6E90] still counting down -> wait
        run_boss_hit = not _l6_tail_6f8f(ov)
    else:
        _l6_spawn_state_machine(ov)                       # [asm 6E92-6F37]
        if g.respawn_state != 0:                          # [asm 6F3C-6F43]
            return _l6_finish(ov)                         # [asm 6F43 jmp 70CB]
        hit, hb = hitbox_overlap(ov.rb, ov.rw, 0x4F1C, 0x5690)   # [asm 6F4C]
        ov.apply(hb)
        bounced = hit
        if not hit:
            hit, hb = hitbox_overlap(ov.rb, ov.rw, 0x4F1C, 0x567E)   # [asm 6F54]
            ov.apply(hb)
            bounced = hit
        if bounced:                                       # [asm 6F4F jb / 6F57 jae 6F8F]
            _l6_bounce_6f59(ov)                           # [asm 6F59]
        else:
            run_boss_hit = not _l6_tail_6f8f(ov)          # [asm 6F57 jae 6F8F]

    if run_boss_hit:
        _l6_boss_hit(ov)                                  # [asm 6FBB-70A4]
        if _s16(p.x) > 0x3D8:                             # [asm 70A5 jle 70CB] the right-boundary crush
            p.death_state = 0x2C; g.anim_gate = 0; p.yvel = 0xFF70   # [asm 70AD-70B7]
            p.motion_mode = 3; p.xvel = 0xFF60            # [asm 70BD-70C2]
            ov.apply(hurt_effect(ov.rb, ov.rw))           # [asm 70C8] 824D

    return _l6_finish(ov)                                 # [asm 70CB-70D6]
