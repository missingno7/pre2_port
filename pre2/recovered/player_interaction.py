"""The player<->world interaction pass (1030:8295..8617) — main-loop subsystem (called at 0x0232).

Two loops over the player (0x4f1c): loop1 = player-vs-enemy collision (stomp / hurt / die), loop2 = player-vs
-entity pickup/powerup dispatch (~25 id handlers). Both reuse the combat island's recovered ``hitbox_overlap``
+ ``death_handler``. This module recovers the island bottom-up; each block is annotated with its ``[asm
<offset>]`` origin and proven byte-exact in shadow (pre2/probes/probe_player_interaction.py).

Recovered so far (the shared keystones used by both loops + the loop2 handlers):
- ``spawn_pickup_effect`` (8875) — add score + spawn a popup/sparkle effect, consume a linked entity.
- ``advance_anim_script`` (80CB) — advance an object's anim-script to its next section (the stomp/dying anim).
"""
from __future__ import annotations

from pre2.recovered.combat_interaction import (death_handler, hitbox_overlap, roll_bonus_sprite,
                                               spawn_effect_burst, _Overlay)
from pre2.recovered.player_collision import _offcamera_trigger
from pre2.recovered.prng import rng_lcg
from pre2.views.dgroup_view import (DebrisSlot, DictBackend, EffectParticle, LightFadeView, ObjectDef, ObjectSlot,
                                    PlayerGlobals, PlayerView, RenderSlot, RngView, WallMarker,
                                    WidthContractBackend)
from pre2.views.tables import Tables

EFFECT_LIST = 0x5450        # [asm 8897] 16-slot popup/effect spawn list, stride 0x12 (= DebrisSlot's arena)
EFFECT_LIST_COUNT = 0x10
ENTITY_LIST_START = 0x50A8  # [asm 88BD] si >= this => a loop2 entity (consume its [+9] linked entity)
ANIM_SECTION_MARKER = 0x7D00


def spawn_pickup_effect(rb, rw, eff_id: int, src_si: int) -> dict:
    """Recover ``1030:8875`` — feedback for collecting/hitting something: add score for a collectible id and
    spawn a popup/sparkle effect entity at the source position; for an entity pickup (``src_si`` in the
    0x50A8 list) also consume its linked entity ``[src_si+9]``. ``eff_id`` = the effect/sprite id (``ax`` at
    the call), ``src_si`` = the source object/entity. Returns the DS ``{offset:(value,width)}`` writes."""
    out: dict = {}
    be = WidthContractBackend(rb, rw, out)
    src = RenderSlot(be, src_si & 0xFFFF)
    # [887B] score-add for collectible ids 0x4A..0x5A (sub bx,0x4a; jb skip; cmp bx,0x10; ja skip)
    bx = (eff_id - 0x4A) & 0xFFFF
    if bx <= 0x10:
        val = Tables(rb, rw).score_value(bx << 1)               # [8887] table word
        g = PlayerGlobals(be)
        score = (g.score_lo | (g.score_hi << 16)) + val   # [888B/888F] 32-bit add
        g.score_lo = score & 0xFFFF
        g.score_hi = (score >> 16) & 0xFFFF
    # [8894] allocate a free effect slot ([+4]==0xFFFF)
    slot = None
    for k in range(EFFECT_LIST_COUNT):
        base = (EFFECT_LIST + k * 0x12) & 0xFFFF
        if RenderSlot(be, base).sprite == 0xFFFF:
            slot = base
            break
    if slot is None:                                           # [88A5] no slot -> only the score landed
        return out
    dst = RenderSlot(be, slot)
    dst.sprite = eff_id & 0xFFFF                                # [88A7] effect id
    dst.x = src.x                                                # [88AA] X = [src]
    dst.y = src.y                                                # [88AE] Y = [src+2]
    DebrisSlot(be, slot).lifetime = 0x2C                         # [88B4]
    g = PlayerGlobals(be)
    g.spawned_ptr = slot                                         # [88B9]
    if (src_si & 0xFFFF) >= ENTITY_LIST_START:                 # [88BD] loop2 entity -> consume the link
        link = src.source
        if link != 0xFFFF:                                     # [88C7]
            RenderSlot(be, link & 0xFFFF).sprite = 0xFFFF       # [88CC]
    return out


def advance_anim_script(rw, di: int) -> dict:
    """Recover ``1030:80CB`` — advance object ``[di]``'s anim-script pointer ``[di+0xC]`` past the next
    ``0x7D00`` section marker (used on a stomp to switch the enemy to its squashed/dying animation)."""
    out: dict = {}
    obj = ObjectSlot(WidthContractBackend(rw, rw, out), di)
    si = obj.anim_ptr
    while True:                                                # [80CF] si += 2 until [si]==0x7D00
        si = (si + 2) & 0xFFFF
        if rw(si) == ANIM_SECTION_MARKER:
            break
    obj.anim_ptr = (si + 2) & 0xFFFF                           # [80D7] past the marker
    return out


# --- loop1: player-vs-enemy collision (829F..83D4) — stomp / hurt / die ---------------------------------
OBJ_BASE = 0x4FD0          # the 12-slot object list
PLAYER = 0x4F1C            # player X (si in loop1)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _knockback(rb, rw, yvel: int) -> dict:
    """[asm 837A] knock the player up: Yvel=yvel, clear [0x6BD2], player Y -= [0xA331]."""
    out: dict = {}
    be = WidthContractBackend(rb, rw, out)
    pv, g = PlayerView(be), PlayerGlobals(be)
    pv.yvel = yvel & 0xFFFF
    g.fall_frames = 0
    pv.y = (pv.y - g.hit_detail) & 0xFFFF
    return out


def _hurt(rb, rw, di):
    """[asm 8348] player hurt by object ``di``: hurt sfx (3) + an escalating hit-counter effect + knockback.
    Returns ``(writes, sfx)``."""
    out = {}
    sfx = [3]                                              # [834B] play_sfx(3)
    obj = ObjectSlot(WidthContractBackend(rb, rw, out), di)
    hc = obj.hits >> 2                                     # [834E] hit count
    if hc != 0xB:                                          # [8355] cap
        obj.hits = (obj.hits + 4) & 0xFF                   # [8359]
        cnt = (hc + 1) & 0xFF                              # [835D] inc
        if (cnt & 1) == 0:                                 # [8360] shr/jnb -> only on even counts
            eff = Tables(rb, rw).hurt_sfx_word(cnt)         # [8366]
            out.update(spawn_pickup_effect(rb, rw, eff, PLAYER))   # [836A]
    g = PlayerGlobals(DictBackend(rb, rw))
    yvel = 0xFF20 if g.in_up != 0 else 0xFFC0              # [836D/8377]
    out.update(_knockback(rb, rw, yvel))
    return out, sfx


def _death(rb, rw, di):
    """[asm 838A] player killed by object ``di``: death sfx (9) + death-state + off-camera respawn trigger."""
    out = {}
    sfx = [9]                                              # [838D] play_sfx(9)
    be = WidthContractBackend(rb, rw, out)
    obj = ObjectSlot(be, di)
    defp = obj.def_ptr                                     # [8390] di = [di+6] (the type def)
    defv = ObjectDef(be, defp)
    defv.d4 = defv.d4 & 0xFE                                # [8393]
    p, g2 = PlayerView(be), PlayerGlobals(be)
    p.death_state = 0x2C                                    # [8397]
    g2.anim_gate = 0                                        # [839C]
    p.yvel = 0xFF80                                          # [83A1]
    p.xvel = (-(((p.xvel & 0xFFFF) << 2) & 0xFFFF)) & 0xFFFF   # [83A7] = -(Xvel<<2)
    g2.low_gravity = 0                                       # [83B3]
    old = g2.glider                                        # [83B8] cmp before the clear
    g2.glider = 0                                            # [83BD]
    if old == 0:                                           # [83C2] jne skip
        n = (g2.energy - 1) & 0xFF                         # [83C4] dec
        g2.energy = n
        if n & 0x80:                                       # [83C8] jns skip -> call only if went negative
            # 65B3 returns byte-level {off:value}; out holds (val,width) tuples -> wrap as width-1
            out.update({o: (v, 1) for o, v in _offcamera_trigger(rb).items()})   # [83CA]
    return out, sfx


def _stomp(rb, rw, di):
    """[asm 82F7] player stomps object ``di`` (attacking + falling fast): spawn effect, mark stomped, and on
    the 3rd stomp (``[di+0x10]&3 == 2``) kill it (squash anim + bounce velocities); else knock the player up."""
    out = {}
    _be = WidthContractBackend(rb, rw, out)
    obj = ObjectSlot(_be, di); pv = PlayerView(_be)
    v10 = obj.hits
    dl = v10 & 3
    out.update(spawn_pickup_effect(rb, rw, ((dl << 1) + 0x52) & 0xFFFF, PLAYER))   # [82F7..8304]
    obj.flags = obj.flags | 0x40                           # [8307] mark stomped
    if dl == 2:                                            # [830B] kill
        obj.state = 0xFF                                   # [8310]
        out.update(advance_anim_script(rw, di))            # [8314] 80CB
        defv = ObjectDef(_be, obj.def_ptr)
        defv.d4 = defv.d4 & 0xF7                             # [831A]
        obj.yvel = 0xFF38                                  # [831E] object Yvel up
        ax = abs(_s16(pv.yvel))                            # [8323] |player Yvel|
        if not (_s16(obj.x) > _s16(pv.x)):                 # [8330] obj left of player -> push left
            ax = -ax
        obj.xvel = (ax * 3) & 0xFFFF                       # [8336] object Xvel = 3*(+/-|Yvel|)
        return out, []
    obj.hits = (v10 + 1) & 0xFF                            # [8340]
    out.update(_knockback(rb, rw, 0xFFA0))                 # [8343] ax=0xffa0 -> 837A
    return out, []


def _loop1_hit_outcome(rb, rw, di):
    """[asm 82C8..82F7] dispatch a player-vs-object hit (instant-death case handled by the walk). Returns
    ``(writes, sfx)``."""
    g = PlayerGlobals(DictBackend(rb, rw))
    if g.hit_flag == 0 or (_s16(PlayerView(DictBackend(rb, rw)).yvel) < 0):   # [82D5 je / 82DF] die: no
        return _death(rb, rw, di)                        # survivable vertical-detail (jne skips) OR [0x4F2B]<0
    if g.low_gravity == 0:                                            # [82E9] hurt
        return _hurt(rb, rw, di)
    if _s16(PlayerView(DictBackend(rb, rw)).yvel) <= 0x20:            # [82F0] not falling -> bump
        return _knockback(rb, rw, 0xFFA0), []
    return _stomp(rb, rw, di)                                         # [82F7] stomp


def loop1(rb, rw, apply, emit_sfx):
    """[asm 829F..83D4] walk the 12 object slots vs the player; on the first qualifying overlap, an
    instant-death object runs ``death_handler`` and the walk CONTINUES, any other outcome
    (death/hurt/stomp/bump) applies + returns. ``apply({off:(val,width)})`` commits writes (so a later
    spawn's find-free sees earlier ones); ``emit_sfx(idx)`` plays a sound. Returns ``early_ret`` — True means
    the 8295 routine returns here (loop2 is skipped)."""
    be = DictBackend(rb, rw)
    g = PlayerGlobals(be); pv = PlayerView(be)
    if pv.death_state != 0:                               # [8295] already dying -> straight to loop2
        return False
    di = OBJ_BASE
    for _ in range(12):                                    # [82A5] cx=0xC
        obj = ObjectSlot(be, di)
        defp = obj.def_ptr
        defv = ObjectDef(be, defp)
        if (obj.sprite != 0xFFFF and (obj.flags & 0x20)     # [82A8..82B2]
                and not (defv.d4 & 0x10)                   # [82B7]
                and obj.state != 0xFF):                    # [82BD]
            hit, hb = hitbox_overlap(rb, rw, PLAYER, di)   # [82C3] 8D7B
            apply(hb)
            if hit:
                if g.scale_level != 0:                     # [82C8] scale/zoom active -> instant death, keep walking
                    # 8C72 returns byte-level {off:value}; loop1's apply wants (val,width) tuples
                    apply({o: (v, 1) for o, v in death_handler(rb, rw, defp, di, PLAYER).items()})   # [82CF]
                else:
                    writes, sfx = _loop1_hit_outcome(rb, rw, di)
                    apply(writes)
                    for s in sfx:
                        emit_sfx(s)
                    return True
        di = (di + 0x12) & 0xFFFF                          # [83CE]
    return False


# --- loop2: player-vs-entity pickups (83D7..8617) — the ~23 effect handlers (names per cyxx level.c) -------
# Offsets confirmed from the ASM handler bodies (cross-checked vs cyxx level_update_player_collision):
ENTITY2 = 0x50A8           # the 52-entry pickup/entity list (objects 23+i in cyxx)
CLUB_TYPE = 0x7B18        # equipped club/weapon type (0..3)  [tests: output-contract key]
LETTERS_MASK = 0x6CA7     # BONUS letters bitmask  [tests: output-contract key]
LIGHT_STATE = 0x6C04      # 0=on,1=off  [tests: output-contract key]
LEVEL = 0x2D8A            # level number  [tests: output-contract key]
LEVEL_DONE = 0x6BE6       # 1=level complete, 0xFF=game complete  [tests: output-contract key]
WALL_MARKER_TABLE = 0x6EA9 # per-column wall/scenery marker table (stride 8)
OBJ_LIST = 0x4FD0        # the 12-slot object (enemy) list
OBJ_COUNT = 12


def _consume_link(rw, si):                                  # [853F] level_clear_item: consume [si+9] entity
    bx = RenderSlot(WidthContractBackend(rw, rw), si & 0xFFFF).source
    return {} if bx == 0xFFFF else {(bx + 4) & 0xFFFF: (0xFFFF, 2)}


def _count_and_score(rb, rw, si, num):
    """[85B6] shared food/collectible tail: bump the item count + add the lut score (spawned at 0x4A+lut)."""
    out = {}
    be = WidthContractBackend(rb, rw, out)
    idx = (num - 0x39) & 0xFFFF                             # num-57
    g = PlayerGlobals(be)
    g.item_queue[idx] = (g.item_queue[idx] + 1) & 0xFF
    g.item_total = (g.item_total + 1) & 0xFFFF
    eff = (Tables(rb).score_spr_lut[idx] + 0x4A) & 0xFFFF
    if RenderSlot(be, si & 0xFFFF).source != 0xFFFF:        # [85CC] linked -> bump the linked count
        g.collected_linked = (g.collected_linked + 1) & 0xFFFF
    out.update(spawn_pickup_effect(rb, rw, eff, si))        # [860B] spawn at eff id
    return out


def _food_fountain(ov):
    """[asm 94F3] erupt 4 food sprites from [0xA336]/[0xA338] with an alternating-spread fountain velocity.
    Each draws a random food sprite id (8C13 :func:`roll_bonus_sprite_id`) into [0xA33A] then bursts one
    entity (8D1B :func:`spawn_effect_burst`). ``ov`` is the bomb's read-through :class:`_Overlay`."""
    ax, dx = 0x20, 0xFF60                                  # [94F9/94FC] initial X/Y velocity
    for _ in range(4):                                    # [94F6] cx=4
        sid = roll_bonus_sprite(RngView(ov))              # [9504] 8C13 advances the rng state (via the view)
        PlayerGlobals(ov).burst_sprite = sid              # [9507] burst sprite id
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, ax, dx, 1))   # [950B] 8D1B, one sprite
        ax = (-ax) & 0xFFFF                               # [950E] neg ax (alternating spread)
        if not (ax & 0x8000):                             # [9510] js not taken (positive) -> step down
            ax = (ax - 0x10) & 0xFFFF                     # [9512]
            dx = (dx - 0x10) & 0xFFFF                     # [9515]


def _bone_burst(ov):
    """[asm 867E] burst ``6*ENERGY + bonus-energy-ctr`` bone sprites (id 0x2046) at (playerX, playerY-0x30)
    via 8D1B :func:`spawn_effect_burst`, then zero ENERGY + the bonus counter. ``ov`` is a read-through
    :class:`_Overlay`. (Used by the trap 864F and the boss-projectile 8618.)"""
    g, pv = PlayerGlobals(ov), PlayerView(ov)
    g.burst_x = pv.x                                       # [867F/8682] [0xA336] = player X
    g.burst_y = (pv.y - 0x30) & 0xFFFF                     # [8685/8688/868B] [0xA338] = player Y - 0x30
    cnt = ((6 * g.energy) + g.hurt_cooldown) & 0xFF         # [868E mul 6,[27d6]] + [8696 add cl,[6bc9]]
    if cnt == 0:                                           # [869A] je -> nothing to scatter
        return
    g.energy = 0                                           # [86A2] [0x27D6] = 0
    g.hurt_cooldown = 0                                     # [86A7] [0x6BC9] = 0
    g.burst_sprite = 0x2046                                # [86AC] burst sprite id
    ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0x30, 0xFF80, cnt))  # [86B2] 8D1B


def _kill_all_screen(rb, rw, si, per_enemy, rng=None):
    """[asm 86B7/870A shared walk] walk the 12 object slots (0x4FD0); for every on-screen enemy
    (``[di+4]!=-1`` & ``![def+4]&0x10`` (def=`[di+6]`) & ``[di+5]&0x20``) run ``per_enemy(ov, di)``; finally
    consume the linked entity ``[si+9]``. Composed over a read-through :class:`_Overlay` (so the per-enemy
    bursts see each other's slot/rng/pos writes). Returns the overlay; the caller adds the spawn-effect tail.
    The leading ``play_sfx(0)`` is returned as the handler's sfx, not a memory write. ``rng`` (optional): the
    live ``pre2/game.Rng`` — registered here so it's visible to every ``per_enemy(ov, di)`` call (they share
    this overlay)."""
    ov = _Overlay(rb)
    if rng is not None:
        ov.register(RngView, rng)
    di = OBJ_LIST
    for _ in range(OBJ_COUNT):                            # [86C9/871C] cx=0xC
        obj = ObjectSlot(ov, di)
        if obj.sprite != 0xFFFF:                          # [86CC/871F] slot active
            bx = obj.def_ptr                              # [86D2/8725] def ptr
            if not (ObjectDef(ov, bx).d4 & 0x10) and (obj.flags & 0x20):  # [86D5/86DB]
                per_enemy(ov, di)                        # [86E1/8734] death_handler / fountain
        di = (di + 0x12) & 0xFFFF                         # [86E4/874B]
    link = RenderSlot(ov, si & 0xFFFF).source             # [86F7/8759] consume the linked entity
    if link != 0xFFFF:
        RenderSlot(ov, link & 0xFFFF).sprite = 0xFFFF
    return ov


def loop2_handler(num, rb, rw, si, find_free, rng=None):
    """Dispatch a pickup hit (ax=num=(spr_num&0x1FFF)-0x35) to its effect, in the ASM's chain order. Returns
    (writes, sfx). Every effect path is now recovered + verified (the trap 864F was the last ASM_MATCHED-only
    one, verified byte-exact on the skull witness 202721); an unmapped id is a no-op (ASM 84F3). (Names per
    cyxx level.c.) ``rng`` (optional): the live ``pre2/game.Rng`` forwarded to the two RNG-touching paths
    (the yvel-bounce popup and the bomb's food fountains)."""
    be = DictBackend(rb, rw)
    g = PlayerGlobals(be); pv = PlayerView(be)             # read-only named access
    if num == 0x91:                                        # id 0xc6 [885F] "tap": clear fly timers, then count
        out = {}
        wbe = WidthContractBackend(rb, rw, out)
        for k in range(0x14):                              # [8861] table 0x6EA9, 0x14 * 8
            WallMarker(wbe, (WALL_MARKER_TABLE + k * 8) & 0xFFFF).b7 = 7
        out.update(_count_and_score(rb, rw, si, num))
        return out, [8]
    if num == 0xE2:                                       # id 0x117 [882A] end-of-level (level transition)
        lvl = g.level; out = {}
        nxt = {2: 0xC, 0xD: 2, 6: 0xE, 0xF: 6}.get(lvl)
        wbe = WidthContractBackend(rb, rw, out)
        g2 = PlayerGlobals(wbe)
        if nxt is not None:
            g2.level = nxt
        g2.level_end_mode = 1
        return out, []
    if num == 0x102:                                      # id 0x137 [8859] game complete
        out = {}
        PlayerGlobals(WidthContractBackend(rb, rw, out)).level_end_mode = 0xFF
        return out, []
    if num == 0xE4:                                       # id 0x119 [87FD] checkpoint
        out = {}
        wbe = WidthContractBackend(rb, rw, out)
        g2 = PlayerGlobals(wbe)
        g2.checkpoint_x = pv.x; g2.checkpoint_y = pv.y
        for k in range(0x46):                             # [8809] reveal item 0x118 in the 0x8F1D table
            src = g2.effect_sources[k]
            if src.sprite == 0x118:
                src.sprite = 0x119
        bx = RenderSlot(wbe, si & 0xFFFF).source
        if bx != 0xFFFF:
            link = RenderSlot(wbe, bx & 0xFFFF)
            link.sprite = (link.sprite - 1) & 0xFFFF
        return out, []
    if num == 0xAE:                                       # id 0xe3 [87E6] extra life (+1 life, spawn effect)
        out = {}
        lives = g.lives                                   # [65D6] 65DA cmp [0x27D8],0x63
        if lives < 0x63:                                  # [65DF je / 65E1 jb] <99 -> inc (==99 caps; the
            PlayerGlobals(WidthContractBackend(rb, rw, out)).lives = (lives + 1) & 0xFF   #   65E3 >99 cs:[0x26FA] self-mod path is unreachable)
        out.update(spawn_pickup_effect(rb, rw, 0xE3, PLAYER))   # [87F6] 0xe3 effect at the player pos
        out.update(_consume_link(rw, si))                 # [87FA] jmp 853F consume the linked entity
        return out, [4]
    if num in (0xD, 0xB6, 0x2C, 0xE0):                    # ids 0x42/0xeb/0x61/0x115 [87AE..] club/weapon 0-3
        ct = {0xD: 0, 0xB6: 1, 0x2C: 2, 0xE0: 3}[num]
        w = {}
        PlayerGlobals(WidthContractBackend(rb, rw, w)).attack_phase = ct
        w.update(_consume_link(rw, si))
        return w, [8]
    if num <= 0x14:                                       # ids 0x35-0x49 [85DA] small energy bonus
        out = dict(_consume_link(rw, si))
        wbe = WidthContractBackend(rb, rw, out)
        g2 = PlayerGlobals(wbe)
        ctr = (g.hurt_cooldown + 1) & 0xFF
        g2.hurt_cooldown = ctr
        if ctr >= 6 and g.energy != 3:
            g2.energy = (g.energy + 1) & 0xFF
            g2.hurt_cooldown = 0
            out.update(spawn_pickup_effect(rb, rw, 0xE2, si))
        return out, [8]
    if num <= 0x2C:                                       # ids 0x4a-0x60 [8524] BONUS letters
        out = {}
        idx = (num - 0x27) & 0xFFFF
        if 0 <= idx <= 4:
            PlayerGlobals(WidthContractBackend(rb, rw, out)).bonus_letters = g.bonus_letters | (1 << idx)
        out.update(_consume_link(rw, si))
        return out, [8]
    if num <= 0x32:                                       # ids 0x62-0x67 [854F] utensils/tools
        out = {}
        wbe = WidthContractBackend(rb, rw, out)
        idx = (num - 0x2D) & 0xFF
        g2 = PlayerGlobals(wbe)
        g2.utensils_mask = g.utensils_mask | (1 << idx)
        if idx == 1:                                      # lighter -> reveal the 0x116 semaphore item
            for k in range(0x46):
                src = g2.effect_sources[k]
                if src.sprite == 0x116:
                    src.sprite = 0x117
        out.update(_consume_link(rw, si))
        return out, [8]
    if num <= 0x40:                                       # ids 0x68-0x75 [8582] food (bounce or score)
        item = EffectParticle(be, si & 0xFFFF)
        ydir = _s16(item.yvel)
        if ydir < 0x80:                                   # low -> count + score (shared 85B6)
            return _count_and_score(rb, rw, si, num), [4]
        out = {}
        wbe = WidthContractBackend(rb, rw, out)
        if rng is not None:
            wbe.register(RngView, rng)
        item2 = EffectParticle(wbe, si & 0xFFFF)
        item2.yvel = (-ydir) & 0xFFFF                     # bounce up
        ret = RngView(wbe).roll()                         # [asm call 39DF] advance + write back the LCG
        xv = 0x20
        if ret & 1:
            xv = (-0x20) & 0xFFFF
            PlayerGlobals(wbe).camera_shake = 7
        item2.xvel = xv
        item2.sprite = g.burst_sprite
        return out, [4]
    if num <= 0x4A:                                       # ids 0x76-0x7f [850A] flying power-up
        out = {}
        if g.glider == 0:
            wbe = WidthContractBackend(rb, rw, out)
            PlayerGlobals(wbe).glider = 1
            PlayerView(wbe).anim_b = 0xFF
            out.update(_consume_link(rw, si))
        return out, [8]
    if num <= 0xA6:                                       # ids 0x80-0xdb [85B0] collectibles -> score
        return _count_and_score(rb, rw, si, num), [8]
    if num == 0xAD:                                       # id 0xe2 [84F6] energy refill (+1 if < 3)
        out = {}
        if g.energy < 3:
            PlayerGlobals(WidthContractBackend(rb, rw, out)).energy = (g.energy + 1) & 0xFF
            out.update(_consume_link(rw, si))
            return out, [4]
        return out, []                                    # full -> nothing (the 8509 ret)
    if num in (0xA7, 0xA8):                               # ids 0xdc/0xdd [864F] trap hit (scatter bones)
        ov = _Overlay(rb)                                 # VERIFIED byte-exact on the skull-trap witness 202721
        #                                                   (whole-pass shadow: predicted writes + completeness)
        p, g3 = PlayerView(ov), PlayerGlobals(ov)
        p.death_state = 0x2C                              # [8655] enter hurt/death state
        p.run_flag = 0                                        # [865A]
        p.anim_b = 8                                      # [865F] anim 8
        _bone_burst(ov)                                   # [8664] 867E scatter the player's bones
        g3.camera_shake = 7                                # [8667]
        link = RenderSlot(ov, si & 0xFFFF).source          # [866C] consume the linked entity
        if link != 0xFFFF:
            RenderSlot(ov, link & 0xFFFF).sprite = 0xFFFF
        ov.apply(spawn_pickup_effect(ov.rb, ov.rw, 0xE4, si))   # [8679] ax=0xe4 -> 860B
        return {o: (v, 1) for o, v in ov.writes.items()}, [1]
    if num == 0xA9:                                       # id 0xde [86B7] grenade: kill every on-screen enemy
        def _grenade(ov, di):                             # [86E1] each enemy dies via the recovered 8C72
            ov.merge_bytes(death_handler(ov.rb, ov.rw, ObjectSlot(ov, di).def_ptr, di, si))  # 8C72 = byte-level
        ov = _kill_all_screen(rb, rw, si, _grenade, rng=rng)
        PlayerGlobals(ov).camera_shake = 9                 # [86E9] screen shake
        ov.apply(spawn_pickup_effect(ov.rb, ov.rw, 0xE6, si))   # [8704] ax=0xe6 -> 860B
        return {o: (v, 1) for o, v in ov.writes.items()}, [0]
    if num == 0xAA:                                       # id 0xdf [870A] bomb: kill all + food fountains
        def _bomb(ov, di):                                # [8734] erase the enemy + erupt a fountain at its pos
            g4 = PlayerGlobals(ov)
            slot = ObjectSlot(ov, di)
            g4.burst_x = slot.x                           # [8736]
            g4.burst_y = slot.y                            # [873C]
            slot.state = 0xFF                              # [873F] mark dead
            slot.sprite = 0xFFFF                           # [8743] free the slot
            _food_fountain(ov)                            # [8748] 94F3
        ov = _kill_all_screen(rb, rw, si, _bomb, rng=rng)
        ov.apply(spawn_pickup_effect(ov.rb, ov.rw, 0xE7, si))   # [8766] ax=0xe7 -> 860B
        return {o: (v, 1) for o, v in ov.writes.items()}, [0]
    if num == 0xB5:                                       # id 0xea [876C] light OFF
        out = {}
        lf = LightFadeView(be)
        if lf.lights_off != 1:
            wbe = WidthContractBackend(rb, rw, out)
            lf2 = LightFadeView(wbe)
            lf2.to_light = 0; lf2.to_dark = 1; lf2.step = 0; lf2.lights_off = 1
            out.update(_consume_link(rw, si)); return out, [1]
        out.update(_consume_link(rw, si)); return out, []
    if num == 0xB4:                                       # id 0xe9 [8790] light ON
        out = {}
        lf = LightFadeView(be)
        if lf.lights_off != 0:
            wbe = WidthContractBackend(rb, rw, out)
            lf2 = LightFadeView(wbe)
            lf2.to_dark = 0; lf2.to_light = 1; lf2.step = 0; lf2.lights_off = 0
        out.update(_consume_link(rw, si)); return out, []
    return {}, []                                         # [asm 84F3 jmp 860E] an unmapped id is a NO-OP: the
    #                                                       entity was already consumed (8426); the ASM just
    #                                                       falls through the dispatch to the loop advance.


def _boss_projectile(rb, rw):
    """[asm 8618] boss-projectile hit (loop2 ids 0x1CA/0x1CB). ENERGY==0 -> 65B3 _offcamera_trigger (lose a
    life / game over); else enter the hurt state, lose 1 energy, and 867E bone-burst (forced to a 6-bone
    scatter). No pickup-effect / no link-consume — the ASM jmps straight to the loop advance. Returns
    (writes, sfx). VERIFIED byte-exact on the final-boss witness 213544 (recon 8618->860E: 101 writes, 0 div)."""
    ov = _Overlay(rb)
    g, p = PlayerGlobals(ov), PlayerView(ov)
    if g.energy < 1:                                       # [8618] cmp [0x27D6],1 ; jae
        ov.merge_bytes(_offcamera_trigger(ov.rb))         # [861F] 65B3 death/respawn (byte-level dict)
        return {o: (v, 1) for o, v in ov.writes.items()}, []
    p.death_state = 0x2C                                    # [862A] hurt state
    p.run_flag = 0                                              # [862F]
    p.anim_b = 8                                            # [8634] anim 8
    e = (g.energy - 1) & 0xFF                              # [8639] dec [0x27D6]
    g.energy = 1                                            # [8641] force ENERGY=1 so 867E scatters 6 bones
    _bone_burst(ov)                                        # [8646] 867E (also zeroes ENERGY + bonus)
    g.energy = e                                            # [864A] restore the decremented energy
    return {o: (v, 1) for o, v in ov.writes.items()}, [1]


_EARLY_SKIP = (0xE5, 0x12C, 0x132, 0x134, 0x136)          # [840A] ids that pass through (no consume, no effect)


def loop2(rb, rw, apply, emit_sfx, find_free, rng=None):
    """[asm 83D7..8617] walk the 52-entry pickup list (0x50A8) vs the player; on a hitbox overlap of a
    collectible (`[si+5]&0x20`) entity, consume it and dispatch its effect. Applies writes via ``apply``;
    plays sounds via ``emit_sfx``. Every effect path (incl. the boss-projectile 8618) is now recovered +
    verified byte-exact, so nothing fails loud. ``rng`` (optional): the live ``pre2/game.Rng`` forwarded to
    :func:`loop2_handler`."""
    si = ENTITY2
    be = DictBackend(rb, rw)
    for _ in range(0x34):                                  # cx=0x34 (52)
        item = EffectParticle(be, si)
        sid = item.sprite
        if (sid != 0xFFFF and _s16(item.lifetime) <= 0xBC   # [83E0/83E9] live + not-yet-active
                and (item.flags & 0x20)):                            # [83F3] collectible flag
            hit, hb = hitbox_overlap(rb, rw, si, PLAYER)             # [83FC] 8D7B (si=entity, di=player)
            apply(hb)
            if hit:
                aid = sid & 0x1FFF                                   # [8404/8407]
                if aid not in _EARLY_SKIP:
                    # [8426] consume: [0xA33A] stores the FULL spr_num (the &0x1FFF mask is applied AFTER,
                    # only for the dispatch), so the 0x2000 collectible flag stays in its high byte.
                    consume_out = {}
                    cbe = WidthContractBackend(rb, rw, consume_out)
                    RenderSlot(cbe, si).sprite = 0xFFFF
                    PlayerGlobals(cbe).burst_sprite = sid
                    apply(consume_out)
                    if aid in (0x1CA, 0x1CB):                        # [8432] boss projectile (8618)
                        writes, sfx = _boss_projectile(rb, rw)
                    else:
                        writes, sfx = loop2_handler((aid - 0x35) & 0xFFFF, rb, rw, si, find_free, rng=rng)
                    apply(writes)
                    for s in sfx:
                        emit_sfx(s)
        si = (si + 0x12) & 0xFFFF                                    # [860E]


def player_interaction_tick(rb, rw, apply, emit_sfx, find_free, rng=None):
    """[asm 8295..8617] the whole player<->world interaction subsystem: loop1 (player-vs-enemy) then, unless
    loop1 took an early return, loop2 (player-vs-pickup). Every path is recovered + verified byte-exact vs the
    ASM (the trap 864F and boss-projectile 8618 — the last ASM_MATCHED-only ones — on the skull/final-boss
    witnesses), so the whole tick runs natively with no fail-loud paths. ``rng`` (optional): the live
    ``pre2/game.Rng`` forwarded to :func:`loop2`."""
    if loop1(rb, rw, apply, emit_sfx):
        return
    loop2(rb, rw, apply, emit_sfx, find_free, rng=rng)
