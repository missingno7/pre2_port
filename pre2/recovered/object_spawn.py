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


@oracle_link("1030:71AB",
             "the camera SEQUENCER: an 8-state machine on [0x91FE] driving the level-intro/boss camera pan. Each "
             "state advances the timer [0xA3F7], gates on the player-cursor distance [0xA3FB] / level param "
             "[0x91FC], sets the active camera-script pointer [0xA401], computes the scroll velocity "
             "[0x6C06]/[0x6C08], and transitions [0x91FE]; composes the recovered inc_scroll_phase (757A) + "
             "camera_boundary_collision (81B4). State 6 is the boss-reach FINALE (disable + 94F3 x4 + burst, RETs "
             "directly) and is gated (Pre2SpawnGap, unrecovered 94F3).",
             "OBSERVED", merge_target="object_spawn")
def camera_state_machine(rb, rw):
    """[asm 71AB..7534] Returns the ``{offset: (value, width)}`` writes at the 7534 exit. Raises
    :class:`Pre2SpawnGap` on the state-6 finale (94F3, which RETs at 74E9 instead of reaching 7534)."""
    state = rb(CAM_STATE)
    dist = rw(DIST_X)
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
        if rb(0x6BD0) != 0 and (rb(0x6BD5) & 7) == 0:      # [asm 7221/7228]
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
        raise Pre2SpawnGap("70D7 camera state-6 boss-reach finale (94F3) unrecovered")   # [asm 7491-74E9]

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
