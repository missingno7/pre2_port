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
from pre2.recovered.combat_interaction import hitbox_overlap, spawn_effect_burst


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


@oracle_link("1030:7585",
             "the shared effect-row list-init: spawn min(cx,8) sprite-0x135 effects into the 8-slot list "
             "0x56A2 (stride 0x12) as a horizontal row — [+4]=0x135, [+0]=X (0xD, step 5), [+2]=Y (0xAA) — "
             "then fill the remaining slots with the 0xFFFF terminator. ``cx`` is the spawn count from the "
             "caller (70D7/6ADD/6D34). Side-effect (excluded from this contract): plays sound 0xD via 0x2CC "
             "when cx>0.",
             "OBSERVED", merge_target="object_spawn")
def init_effect_row(cx):
    """[asm 7585] ``cx`` = the caller's spawn count. Returns the ``{offset: (value, width)}`` 0x56A2 contract."""
    writes: dict[int, tuple[int, int]] = {}
    n = cx if cx < EFFECT_ROW_N else EFFECT_ROW_N        # [asm 758E-7593] cap at 8
    si = EFFECT_ROW_LO
    x = EFFECT_X0
    for _ in range(n):                                   # [asm 759F-75AF] the spawned row
        writes[(si + 4) & 0xFFFF] = (EFFECT_SPRITE, 2)
        writes[si & 0xFFFF] = (x, 2)
        writes[(si + 2) & 0xFFFF] = (EFFECT_Y, 2)
        x = (x + EFFECT_DX) & 0xFFFF
        si = (si + EFFECT_ROW_STRIDE) & 0xFFFF
    for _ in range(EFFECT_ROW_N - n):                    # [asm 75B9-75C1] terminate the rest
        writes[(si + 4) & 0xFFFF] = (0xFFFF, 2)
        si = (si + EFFECT_ROW_STRIDE) & 0xFFFF
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
    """[asm 8182] test one sprite ``si`` against the camera targets; accumulate hitbox + free writes into
    ``writes``; return CF (a hit vs target B, or a freed 0x19C/0x19D vs target A)."""
    if rw((si + 4) & 0xFFFF) == 0xFFFF:                  # [asm 8182] inactive
        return False
    if (rw((si + 4) & 0xFFFF) & 0x1FFF) in TARGET_SPRITES:    # [asm 818F-819A] 0x19C/0x19D -> target A
        hit, hb = hitbox_overlap(rb, rw, si, rw(TARGET_A))    # [asm 819C] 8D7B
        for off, (val, wid) in hb.items():
            writes[off] = (val, wid)
        if hit:                                          # [asm 819F-81A6] free on hit
            writes[(si + 4) & 0xFFFF] = (0xFFFF, 2)
            return True
    hit, hb = hitbox_overlap(rb, rw, si, rw(TARGET_B))   # [asm 81A8-81AC] target B
    for off, (val, wid) in hb.items():
        writes[off] = (val, wid)
    return hit


@oracle_link("1030:80DE",
             "the camera-target collision scan (run every frame from the 70D7 tail): test the player (0x4F0A) "
             "then the 4 projectile slots (0x4F2E) against the camera targets [0xA423]/[0xA425] via the hitbox "
             "test 8D7B; free a 0x19C/0x19D sprite that overlaps target A; stop at the first hit. Composes the "
             "verified hitbox_overlap (8D7B). Contract = its [0xA330]/[0xA331] hitbox writes + any freed slots.",
             "OBSERVED", merge_target="object_spawn")
def scan_camera_targets(rb, rw):
    """[asm 80DE] ``rb``/``rw`` read DGROUP byte/word. Returns the ``{offset: (value, width)}`` write contract."""
    writes: dict[int, tuple[int, int]] = {}
    if _target_collision(rb, rw, SCAN_PLAYER, writes):   # [asm 80DE-80E4] player first
        return writes
    si = SCAN_PROJ
    for _ in range(SCAN_PROJ_N):                          # [asm 80E6-80F4] then the 4 projectiles
        if _target_collision(rb, rw, si, writes):
            break
        si = (si + 0x12) & 0xFFFF
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
TBL_FLOOR = 0x7F5E         # ground-tile property table (xlatb)


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
    if _s16(vy) >= 0:                                     # [asm 7121] scrolling down/still -> tile collide
        col = _sar16(cur_x, 4) & 0xFF
        row = _sar16(cur_y, 4) & 0xFF
        tile = read_tile((col | (row << 8)) & 0xFFFF)     # [asm 7128-713B]
        if rb((TBL_FLOOR + tile) & 0xFFFF) != 0:          # [asm 713E-7144] solid floor
            writes[CURSOR_Y] = ((cur_y & 0xFFF0), 2)      # [asm 7146] snap (and byte [0x9201],0xF0)
            if _s16(vy) != 0:                             # [asm 714B] was scrolling
                writes[SCROLL_STOP_FLAG] = (7, 1)
            writes[SCROLL_VY] = (0, 2)                     # [asm 7157] stop the scroll
            writes[SCROLL_VX] = (0, 2)
    elif not (_s16(vy) >= SCROLL_VY_GRAV_CAP):            # [asm 7165] scrolling up -> gravity
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
HURT_COOLDOWN = 0x6BC9    # damage cooldown; reloads to 5 and costs a life on underflow
LIVES = 0x27D6
HURT_FX_X = 0xA336        # spawn_effect_burst reads this as its SPAWN_X
HURT_FX_Y = 0xA338        # ... SPAWN_Y (player_Y - 0x30)
HURT_FX_SPRITE = 0xA33A   # ... BURST_SPRITE (0x2046)


@oracle_link("1030:824D",
             "the player-hurt effect when the scroll boundary crushes the player (bottom of the 81B4 chain): "
             "tick the damage cooldown [0x6BC9] (reload 5 + lose a life [0x27D6] on underflow -> player death "
             "65B3 when lives deplete), record the hurt-burst origin [0xA336]/[0xA338] = player - (0,0x30), then "
             "spawn one hurt sprite (8D1B, id 0x2046, Xvel +/-0x30 by [0x6BC9] parity, Yvel 0xFF80). Composes "
             "the verified spawn_effect_burst over a read-through overlay so it sees the just-set origin/sprite.",
             "OBSERVED", merge_target="object_spawn")
def hurt_effect(rb, rw):
    """[asm 824D] Returns the ``{offset: (value, width)}`` writes. Raises :class:`Pre2SpawnGap` on the
    (unwitnessed) lives-depleted death path into 65B3."""
    writes = {}
    cd = (rb(HURT_COOLDOWN) - 1) & 0xFF
    if cd & 0x80:                                        # [asm 8254] dec underflowed -> reload + lose a life
        writes[HURT_COOLDOWN] = (5, 1)
        cd = 5
        lives = (rb(LIVES) - 1) & 0xFF
        writes[LIVES] = (lives, 1)
        if lives & 0x80:                                 # [asm 825F] lives underflow -> 65B3 player death
            raise Pre2SpawnGap("824D player death (65B3) unrecovered")
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
    (8D7B always contributes [0xA330]); raises :class:`Pre2SpawnGap` via 824D on the lives-depleted death."""
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
        for off, (val, wd) in writes.items():
            off &= 0xFFFF
            self.writes.pop(off, None)
            self.writes[off] = (val, wd)
            self._bytes[off] = val & 0xFF
            if wd == 2:
                self._bytes[(off + 1) & 0xFFFF] = (val >> 8) & 0xFF


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
    """[asm 81B4] Returns the ``{offset: (value, width)}`` writes; raises :class:`Pre2SpawnGap` via 824D on the
    lives-depleted death path."""
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
