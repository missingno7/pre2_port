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
from pre2.recovered.combat_interaction import hitbox_overlap

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
