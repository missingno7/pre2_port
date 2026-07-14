"""Secondary-entity update pass — the per-frame lifetime/physics tick of the lightweight effect, particle and
projectile lists (the complement of the already-recovered *emitters* in
:mod:`pre2.recovered.combat_interaction` and :mod:`pre2.recovered.player`).

Once per frame the main loop (1030:021A..022C, ``ds=0x1A0F``) runs four small fixed-stride (0x12) slot-array
walkers back-to-back, each ticking one list:

    021A  call 581E   5-slot ring  via [0x6BBE]   — popup/score lifetime + anim
    0223  call 6210   4-slot       @ 0x4F2E       — thrown-weapon projectiles (anim-script + handler dispatch)
    0226  call 60FE   32-slot      @ 0x50A8       — physics particles (gravity / bounce / tile-collide / anim)
    0229  call 60DF   16-slot      @ 0x5450       — debris/effect pool lifetime  (pairs with spawn_debris_element 8875)

Each leaf lands here as a pure ``rw -> {offset: (value, width)}`` write-contract (the
:mod:`pre2.views.effects_update` seam reads DGROUP and applies the writes); recovered leaf-first with shadow
proof, exactly like the object_tick / combat_interaction precedents.
"""
from __future__ import annotations

from pre2.islands import oracle_link
from pre2.views.dgroup_view import (DebrisSlot, EffectParticle, PopupSlot, ProjectileSlot,
                                    WidthContractBackend)
from pre2.views.tables import Tables


class Pre2EffectsGap(Exception):
    """An unrecovered path in the secondary-entity update pass (fail loud; never silently run ASM)."""


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _neg16(v):
    return (-_s16(v)) & 0xFFFF


def _sar16(v, n):
    return (_s16(v) >> n) & 0xFFFF


# --- list 0x5450: the 16-slot debris/effect pool (emitter = combat_interaction.spawn_debris_element @8875) ---
DEBRIS_POOL_LO = 0x5450
DEBRIS_POOL_N = 0x10
STRIDE = 0x12
DEAD = 0xFFFF        # [+4] sentinel for a free slot


@oracle_link("1030:60DF",
             "per-frame lifetime tick of the 16-slot debris/effect pool 0x5450 (stride 0x12): for each active "
             "slot ([+4]!=0xFFFF) decrement the [+2] field and the lifetime [+0xC]; when [+0xC] reaches exactly "
             "0 free the slot ([+4]=0xFFFF). Complement of spawn_debris_element (8875), which arms [+0xC]=0x2C. "
             "An underflow of [+0xC] past 0 wraps to 0xFFFF and the slot survives (replicated faithfully).",
             "VERIFIED", merge_target="effects_update")
def tick_debris_pool(rw):
    """[asm 60DF] ``rw(off)`` reads a DGROUP word. Returns the ``{offset: (value, width)}`` write contract."""
    writes: dict[int, tuple[int, int]] = {}
    be = WidthContractBackend(rw, rw, writes)   # word-only pool: reads via rw, writes accumulate the contract
    b = DEBRIS_POOL_LO
    for _ in range(DEBRIS_POOL_N):
        slot = DebrisSlot(be, b)
        if slot.sprite != DEAD:                                # [asm 60E5] active?
            slot.timer = (slot.timer - 1) & 0xFFFF             # [asm 60EB] dec [+2]
            life = (slot.lifetime - 1) & 0xFFFF                # [asm 60EE] dec [+0xC]
            slot.lifetime = life
            if life == 0:                                      # [asm 60F1] jne -> survive
                slot.sprite = DEAD                             # [asm 60F3] free
        b = (b + STRIDE) & 0xFFFF
    return writes


# --- list via [0x6BBE]: the 5-slot popup/score ring in [0x4F76, 0x4FC0), walked downward (wrap LO->HI) ---
POPUP_RING_PTR = 0x6BBE   # [0x6BBE] = head pointer into the ring
POPUP_RING_LO = 0x4F76    # ring low bound; after si drops below this it wraps to HI
POPUP_RING_HI = 0x4FBE
POPUP_RING_N = 5
POPUP_ANIM_END = 0x3A     # [+4] anim id counts up; on reaching 0x3A the slot is freed (0xFFFF)


@oracle_link("1030:581E",
             "per-frame tick of the 5-slot popup/score ring (head [0x6BBE], walked down by 0x12, wrapping "
             "0x4F76->0x4FBE): each slot decrements its [+2] timer and advances its [+4] anim id (masked "
             "ah&0x1F, then +1); when the id reaches 0x3A the slot frees to 0xFFFF. The id==0xFFFF early-out "
             "(581E 582B-582E) is structurally dead after the ah&0x1F mask, so the body runs every slot.",
             "OBSERVED", merge_target="effects_update")
def tick_popup_ring(rw):
    """[asm 581E] ``rw(off)`` reads a DGROUP word. Returns the ``{offset: (value, width)}`` write contract."""
    writes: dict[int, tuple[int, int]] = {}
    be = WidthContractBackend(rw, rw, writes)   # word-only ring: reads via rw, writes accumulate the contract
    si = rw(POPUP_RING_PTR) & 0xFFFF
    for _ in range(POPUP_RING_N):
        slot = PopupSlot(be, si)
        ax = slot.anim & 0x1FFF                                # [asm 5825-5828] mov ax,[si+4]; and ah,0x1F
        slot.timer = (slot.timer - 1) & 0xFFFF                 # [asm 5831] dec [+2]
        ax = (ax + 1) & 0xFFFF                                 # [asm 5830] inc ax
        slot.anim = ax if ax < POPUP_ANIM_END else 0xFFFF      # [asm 5834-583C]
        si = (si - STRIDE) & 0xFFFF                            # [asm 5841] sub si,0x12
        if si < POPUP_RING_LO:                                 # [asm 5844-584A] wrap
            si = POPUP_RING_HI
    return writes


# --- list 0x50A8: the 32-slot physics-particle pool (the bounce/debris fragments; emitter = combat_interaction
#     burst @0x50A8). Each slot: [+0]=X [+2]=Y [+4]=sprite-id(+flags) [+6]=Xvel [+7]=facing-byte
#     [+0xC]=lifetime(signed) [+0xE]=Yvel [+0x11]=substate. World coords are fixed-point (>>4 = tile/pixel). ---
PARTICLE_LO = 0x50A8
PARTICLE_N = 0x20
GRAVITY = 9                  # [asm 615C] Yvel += 9 / frame
YVEL_CAP = 0x100             # [asm 615F] stop integrating gravity once Yvel+9 would reach 0x100
X_MAX = 0x1000               # [asm 6149] world-X bounce bound
LIFE_CLAMP = 0x32            # [asm 612B] special ids cap their lifetime here
FREEZE_FLAG = 0x6BD5         # [asm 61E3] bit0 -> skip the sprite animation
MAP_SEG_PTR = 0x2DDA         # [asm 6106] [0x2DDA] = the level-map (es) segment
ANIM_WRAP_AT = 0x49          # [asm 61F9] sprite cycles ..->0x49 ->0x46
ANIM_WRAP_TO = 0x46
ID_LIFE_CLAMP = (0x136, 0x134, 0x12C, 0x132)   # [asm 6168] -> clamp lifetime, skip tile/anim
ID_SKIP = 0xE5                                  # [asm 617A] -> skip tile/anim


@oracle_link("1030:60FE",
             "per-frame physics tick of the 32-slot particle pool 0x50A8 (stride 0x12): dec lifetime [+0xC]; "
             "when >0 integrate X/Y by Xvel/Yvel>>4, apply gravity (Yvel+9 capped at 0x100), bounce X at "
             "0/0x1000; then per sprite-id (masked dh&0x1F) either clamp lifetime to 0x32 (ids "
             "0x134/0x136/0x12C/0x132), skip (0xE5), or run tile collision via es=[0x2DDA] + xlatb tables "
             "0x7F5E (floor, when falling) / 0x7E5E (ceiling, when rising) and animate the sprite 0x46..0x49 "
             "(unless [0x6BD5]&1 or Xvel==0). lifetime==0 sets substate [+0x11]=0xF; <0 frees once [+0x11]==0.",
             "OBSERVED", merge_target="effects_update")
def tick_particles(rw, rb, read_tile):
    """[asm 60FE] ``rw``/``rb`` read DGROUP word/byte; ``read_tile(off)`` reads the level-map (es) segment.
    Returns the ``{offset: (value, width)}`` write contract."""
    writes: dict[int, tuple[int, int]] = {}
    tbl = Tables(rb)
    freeze = rb(FREEZE_FLAG) & 1
    b = PARTICLE_LO
    for _ in range(PARTICLE_N):
        pt = EffectParticle(WidthContractBackend(rb, rw, writes), b)
        if pt.sprite == 0xFFFF:                                # [asm 610A] inactive slot
            b = (b + STRIDE) & 0xFFFF
            continue
        life = (pt.lifetime - 1) & 0xFFFF                      # [asm 6110] dec lifetime
        pt.lifetime = life
        sl = _s16(life)
        if sl < 0:                                             # [asm 6117] expired
            if pt.substate == 0:                               # free once the substate marker cleared
                pt.sprite = 0xFFFF                             # [asm 611D]
            b = (b + STRIDE) & 0xFFFF
            continue
        if sl == 0:                                            # [asm 6124] just expired -> arm substate
            pt.substate = 0x0F
            b = (b + STRIDE) & 0xFFFF
            continue

        # [asm 6139] physics (lifetime > 0)
        x = pt.x
        xv = pt.xvel
        y = pt.y
        yv = pt.yvel
        idv = pt.sprite

        x = (x + _sar16(xv, 4)) & 0xFFFF                       # [asm 613C-613E] X += Xvel>>4
        if x & 0x8000:                                        # [asm 6140] jns -> clamp 0 + bounce
            x = 0
            xv = _neg16(xv)
        if x >= X_MAX:                                        # [asm 6149] jb skip else bounce
            xv = _neg16(xv)

        ydelta = _s16(yv) >> 4                                 # [asm 6157] ax = Yvel>>4 (also fall/rise sign)
        y = (y + (ydelta & 0xFFFF)) & 0xFFFF                   # [asm 6159] Y += Yvel>>4
        g = (_s16(yv) + GRAVITY) & 0xFFFF                      # [asm 615C] dx = Yvel + 9
        if _s16(g) < YVEL_CAP:                                # [asm 615F] jge skip store (cap)
            yv = g

        sid = idv & 0x1FFF                                     # [asm 6168] dh &= 0x1F
        new_id = None
        if sid in ID_LIFE_CLAMP:                               # [asm 612B] clamp lifetime, skip rest
            if life > LIFE_CLAMP:                              # unsigned jbe
                pt.lifetime = LIFE_CLAMP
        elif sid == ID_SKIP:                                   # [asm 617A] skip rest
            pass
        else:
            # [asm 618C] tile collision at the (clamped) world position
            col = (_s16(x) >> 4) & 0xFF
            row = (_s16(y) >> 4) & 0xFF
            bx = (col | (row << 8)) & 0xFFFF
            if ydelta > 0:                                     # [asm 619B] falling -> floor check
                if tbl.floor_props[read_tile(bx)] != 0:              # [asm 61A3-61A6] solid
                    yv = _sar16(_neg16(yv), 1)                 # [asm 61A8-61AB] -Yvel/2
                    d = 8 if _s16(xv) >= 0 else -8             # [asm 61B3-61BA]
                    axv = (xv - d) & 0xFFFF                    # [asm 61BC] reduce |Xvel| by 8 toward 0
                    xv = axv if (((axv >> 8) ^ pt.facing) & 0xFF) == 0 else 0   # [asm 61C0-61C5]
            else:                                              # [asm 61CC] rising -> ceiling check
                if tbl.ceil_props[read_tile((bx - 0x100) & 0xFFFF)] != 0:                  # [asm 61D1-61D5]
                    xv = _neg16(xv)                            # [asm 61D9]
                    x = (x + _sar16(xv, 4)) & 0xFFFF           # [asm 61DC-61E1]
            # [asm 61E3] sprite animation 0x46..0x49 (gated by [0x6BD5]&1 and Xvel!=0)
            if not freeze and (xv & 0xFFFF) != 0:
                masked = idv & 0x1FFF
                if masked < ANIM_WRAP_AT:                      # [asm 61FC] jb -> id+1 (keeps flag bits)
                    new_id = (idv + 1) & 0xFFFF
                elif masked == ANIM_WRAP_AT:                   # [asm 6200] wrap
                    new_id = ANIM_WRAP_TO

        pt.x = x
        pt.y = y
        pt.xvel = xv
        pt.yvel = yv
        if new_id is not None:
            pt.sprite = new_id
        b = (b + STRIDE) & 0xFFFF
    return writes


# --- list 0x4F2E: the 4 thrown-weapon projectile slots (the last 2 player weapons are throwable; emitter =
#     player.spawn_projectile @6017). Each slot: [+0]=X [+2]=Y [+4]=sprite-id(+facing bit15) [+5]=flag byte
#     ([+5]&0x20 = "alive") [+6]=Xvel [+7]=facing-byte(bit7) [+0xC]=anim-script ptr [+0xE]=Yvel [+8]=handler. ---
PROJECTILE_LO = 0x4F2E
PROJECTILE_N = 4
PROJ_ALIVE = 0x20            # [asm 621C] [+5] bit; clear -> free the slot
# DS:0x79EC dispatch (only idx 0/1 are real entries; both are 1-instruction Yvel tweaks at 6272/6277):
PROJ_HANDLER_DYV = {0: 0x20, 1: -0x10}   # idx -> delta added to Yvel [+0xE]


@oracle_link("1030:6210",
             "per-frame tick of the 4 thrown-weapon projectile slots 0x4F2E (stride 0x12): free a slot whose "
             "[+5]&0x20 is clear; else integrate X/Y by Xvel/Yvel>>4, advance the anim-script pointer [+0xC] "
             "(a negative script word loops back by that signed offset, then +2), set [+4]=script word | the "
             "facing bit ([+7]&0x80)<<8, and dispatch the [+8] handler (DS:0x79EC: idx0 Yvel+=0x20, idx1 "
             "Yvel-=0x10). Witness: demo …233821 (idx1, 238 update calls). Other handler indices fail loud.",
             "OBSERVED", merge_target="effects_update")
def tick_projectiles(rw, rb):
    """[asm 6210] ``rw``/``rb`` read DGROUP word/byte. Returns the ``{offset: (value, width)}`` write contract."""
    writes: dict[int, tuple[int, int]] = {}
    b = PROJECTILE_LO
    for _ in range(PROJECTILE_N):
        proj = ProjectileSlot(WidthContractBackend(rb, rw, writes), b)
        if proj.sprite == 0xFFFF:                             # [asm 6216] inactive slot
            b = (b + STRIDE) & 0xFFFF
            continue
        if not (proj.alive & PROJ_ALIVE):                     # [asm 621C] dead -> free
            proj.sprite = 0xFFFF
            b = (b + STRIDE) & 0xFFFF
            continue
        proj.x = (proj.x + _sar16(proj.xvel, 4)) & 0xFFFF     # [asm 6229] X += Xvel>>4
        proj.y = (proj.y + _sar16(proj.yvel, 4)) & 0xFFFF     # [asm 6236] Y += Yvel>>4

        ptr = proj.anim_ptr                                   # [asm 6244] anim-script pointer
        word = rw(ptr)
        if word & 0x8000:                                    # [asm 6249] negative -> loop back
            ptr = (ptr + _s16(word)) & 0xFFFF
            word = rw(ptr)
        proj.anim_ptr = (ptr + 2) & 0xFFFF                    # [asm 6251] advance

        facing = proj.facing & 0x80                           # [asm 6256] [+4] = script word | facing bit
        proj.sprite = (word | (facing << 8)) & 0xFFFF

        idx = rb((b + 8) & 0xFFFF)                            # [asm 6261] handler dispatch [+8]
        if idx not in PROJ_HANDLER_DYV:
            raise Pre2EffectsGap(f"6210 projectile handler idx {idx} unrecovered (DS:0x79EC)")
        proj.yvel = (proj.yvel + PROJ_HANDLER_DYV[idx]) & 0xFFFF
        b = (b + STRIDE) & 0xFFFF
    return writes
