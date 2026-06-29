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
:mod:`pre2.bridge.effects_update` seam reads DGROUP and applies the writes); recovered leaf-first with shadow
proof, exactly like the object_tick / combat_interaction precedents.
"""
from __future__ import annotations

from pre2.islands import oracle_link

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
    b = DEBRIS_POOL_LO
    for _ in range(DEBRIS_POOL_N):
        if rw((b + 4) & 0xFFFF) != DEAD:                       # [asm 60E5] active?
            writes[(b + 2) & 0xFFFF] = ((rw((b + 2) & 0xFFFF) - 1) & 0xFFFF, 2)   # [asm 60EB] dec [+2]
            life = (rw((b + 0xC) & 0xFFFF) - 1) & 0xFFFF        # [asm 60EE] dec [+0xC]
            writes[(b + 0xC) & 0xFFFF] = (life, 2)
            if life == 0:                                      # [asm 60F1] jne -> survive
                writes[(b + 4) & 0xFFFF] = (DEAD, 2)           # [asm 60F3] free
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
    si = rw(POPUP_RING_PTR) & 0xFFFF
    for _ in range(POPUP_RING_N):
        ax = rw((si + 4) & 0xFFFF) & 0x1FFF                    # [asm 5825-5828] mov ax,[si+4]; and ah,0x1F
        writes[(si + 2) & 0xFFFF] = ((rw((si + 2) & 0xFFFF) - 1) & 0xFFFF, 2)  # [asm 5831] dec [+2]
        ax = (ax + 1) & 0xFFFF                                 # [asm 5830] inc ax
        writes[(si + 4) & 0xFFFF] = ((ax if ax < POPUP_ANIM_END else 0xFFFF), 2)  # [asm 5834-583C]
        si = (si - STRIDE) & 0xFFFF                            # [asm 5841] sub si,0x12
        if si < POPUP_RING_LO:                                 # [asm 5844-584A] wrap
            si = POPUP_RING_HI
    return writes
