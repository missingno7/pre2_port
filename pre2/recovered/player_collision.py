"""Prehistorik 2 player ground/tile collision — recovered native logic (pure).

The collision routine (`1030:5A96`, called from the player update at `5A41` after the Y integrate) resolves the
player against the tile map: it computes the player's tile cell, dispatches a per-tile-type handler
(`cs:[0x7D9B]`), and on contact snaps Y to the tile boundary, zeroes Yvel, and sets the airborne/state flags.
See docs/pre2/player_collision_island.md for the full boundary map.

This module recovers the island bottom-up, each leaf proven byte-exact in shadow before any live replacement.
Started with the three self-contained leaves (slope offset, fall flag, horizontal block).

READABILITY (the offsets-into-views lift): gameplay logic here reads/writes NAMED state — ``p`` is the
:class:`PlayerView` (x/y/sprite/xvel/yvel/...), ``g`` the :class:`PlayerGlobals` (airborne/fall_frames/
last_land_y/...) — each field's DGROUP offset + width + [asm] evidence lives ONCE, in pre2/views/dgroup_view.
The functions still return the island's plain ``{offset: value}`` write contracts (named assignments record
into them via :class:`DictBackend`), so callers, hooks and the byte-exact proofs are unchanged. Raw offsets
remain only where they are genuinely dynamic data (the null-pointer deref in 668B, the wall-marker slot list,
tile-table indexing through the named table constants)."""
from __future__ import annotations

from pre2.recovered.player import player_emit_trail
from pre2.views.dgroup_view import DictBackend, PlayerGlobals, PlayerView
from pre2.views.tables import Tables

__all__ = ["collision_slope_offset", "collision_fall", "collision_hblock", "collision_land",
           "collision_ceiling", "collision_ground_handler", "collision_bridge_dip",
           "collision_side_handler", "collision_airborne", "collision", "AIRBORNE_FLAG", "TILE_PROP_TABLE",
           "TILE_CEIL_SOLID_TABLE", "TILE_CEIL_HANDLER_TABLE", "GROUND_REMAP_TABLE", "BRIDGE_FLAG_TABLE",
           "DIRTY_KIND_TABLE", "SIDE_FLAG_TABLE", "WALL_MARKER_LIST", "WALL_MARKER_END",
           "PLAYER_ANIM_HEIGHT_TABLE", "COLLISION_BYTE_FIELDS"]

AIRBORNE_FLAG = 0x6BF3   # [asm 6401] the "no ground under the player" flag (= PlayerGlobals.airborne)
TILE_PROP_TABLE = 0x8E1D  # [asm 643C] tile id -> property byte (solid / slope 0x30 / dir 0x10 / height 0x0F)
TILE_CEIL_SOLID_TABLE = 0x7E5E   # [asm 5C20] tile id -> ceiling-solid flag (bit0) for the side-nudge
TILE_CEIL_HANDLER_TABLE = 0x805E  # [asm 5C26] tile id -> ceiling-handler index (cs:[0x7DA9]): 0 noop / 1 head-bump / 2 trigger
GROUND_REMAP_TABLE = 0x7F5E       # [asm 5BA8] tile id -> ground-handler index (cs:[0x7D9B]) for the foot tile
BRIDGE_FLAG_TABLE = 0x805E        # [asm 5BCE] tile id -> bit 0x20 marks a bridge/sag frame (shares the ceiling table)
DIRTY_KIND_TABLE = 0x4DF8         # [asm 5C7B] tile id -> 0 redraw (653D) / >=1 grid-dirty flags
SIDE_FLAG_TABLE = 0x805E          # [asm 6531] tile id -> bit 0x10 marks a side-solid (wall) tile
WALL_MARKER_LIST = 0x6EA9         # [asm 64FA] 10x 8-byte wall-impact markers; slot free when word == 0x55AA
                                 # (UNION region: firefly_sim reads 0x6EA9 as the 20-slot firefly swarm; cyxx: fly_tbl)
WALL_MARKER_END = 0x6F49          # [asm 6525] one past the last marker slot
PLAYER_ANIM_HEIGHT_TABLE = 0x7191  # [asm 5AAF] anim frame -> player vertical extent (scan-loop row count)

# player-record + player-state byte fields the collision handlers write (named once; the width-contract set
# below lists them). The player-record bytes are relative to the player slot base; the rest are the
# PlayerGlobals state bytes (= the like-named PlayerView/PlayerGlobals fields in dgroup_view).
PLAYER_SLOT = 0x4F1C        # the player render/physics record base (= PLAYER_BASE)
PLAYER_Y = PLAYER_SLOT + 2  # player Y
FALL_FRAMES = 0x6BD2        # descending-fall counter
FALL_LATCH = 0x6BD1         # fall-started latch
ANIM_GATE = 0x6BD0          # hold-current-anim / FSM-route gate
FALL_GRACE = 0x6BE0         # coyote-time / fall grace
PENDING_PICKUP = 0x6BE1     # queued pickup id
SHAKE_MAGNITUDE = 0x6BEA    # screen-shake magnitude
LOW_GRAVITY = 0x6BC7        # low-gravity / attack-invuln flag
FLY_TIMER = 0x6BC8          # flight timer
END_SIGNAL = 0x6BE5         # level-end signal
GRID_DIRTY = 0x2DF4         # whole-grid redraw request
PAGE_DIRTY = 0x6BBD         # one-tile direct re-blit page-dirty byte
RESPAWN_STATE = 0x6BE4      # respawn-pending state
ENERGY = 0x27D6             # player energy (0..3)
LIVES = 0x27D8              # 1-up count

# The byte-width DS fields the collision handlers write; every other DS write is a 16-bit word. Used by the
# composition's byte-level overlay so a later read sees an earlier write at the correct width.
COLLISION_BYTE_FIELDS = frozenset({
    PLAYER_SLOT + 8, PLAYER_SLOT + 9,   # the player Xvel word, written a byte at a time
    AIRBORNE_FLAG, FALL_FRAMES, FALL_LATCH, ANIM_GATE, FALL_GRACE, PENDING_PICKUP, SHAKE_MAGNITUDE,
    LOW_GRAVITY, FLY_TIMER, END_SIGNAL, GRID_DIRTY,
    PAGE_DIRTY,                       # bridge-dip 653D direct-redraw page-dirty byte (mov byte [0x6bbd],1)
    RESPAWN_STATE, ENERGY, LIVES,     # off-camera trigger (65B3)
    # wall-marker slot trailing bytes (+4/+5/+7 of each 8-byte record)
    *(s + d for s in range(WALL_MARKER_LIST, WALL_MARKER_END, 8) for d in (4, 5, 7)),
})


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _views(rb, rw, out: dict | None = None):
    """Bind the island's named views over the caller's DS readers. Named WRITES record into ``out`` (or a
    fresh dict) in the island's plain ``{offset: value}`` contract convention; reads see original memory
    only (read-after-write keeps a local, exactly like the hand-built dicts this replaces)."""
    be = DictBackend(rb, rw, out)
    return PlayerView(be), PlayerGlobals(be), be


def collision_slope_offset(prop: int, player_x: int) -> int:
    """Recover the slope-height offset ``1030:661A``.

    For a slope tile (property bits ``0x30`` set), the player's height within the tile depends on its X position
    across the slope: ``quot = (X & 0x0F) // 3``; on an up-slope (``prop & 0x10``) the offset is
    ``quot + (prop & 0x0F)``, otherwise ``(prop & 0x0F) - quot`` — sign-extended to a word. A non-slope tile
    returns ``prop`` unchanged (the ASM leaves ``ax`` untouched on that path). Pure."""
    al = prop & 0xFF
    if (al & 0x30) == 0:                                  # [661A-661C] not a slope -> ax = cwde(prop)
        return _s8(al) & 0xFFFF
    quot = (player_x & 0x0F) // 3                         # [6620-6628] div bl=3
    low = al & 0x0F                                       # [6630 / 6637] and bl,0xF
    if al & 0x10:                                         # [662B] slope direction
        res = (quot + low) & 0xFF                         # [6633] add al,bl
    else:
        res = (low - quot) & 0xFF                         # [663A-663C] neg al ; add bl
    return _s8(res) & 0xFFFF                              # [663E] cwde


def collision_fall(flag: int) -> int:
    """Recover the fall / no-ground response ``1030:6401`` — set the airborne flag bit0. Pure: returns the
    new ``airborne``."""
    return (flag | 1) & 0xFF


def collision_hblock(x: int, xvel: int) -> tuple:
    """Recover the horizontal-block response ``1030:6407`` (wall hit): undo the X step and stop —
    ``X -= sar(Xvel, 4)`` and ``Xvel = 0``. Pure: returns ``(new_x, new_xvel)``."""
    return (x - (_s16(xvel) >> 4)) & 0xFFFF, 0           # [6408-6417]


def _air_drift_x(rw, rb, bp: int) -> int:
    """Air horizontal drift + clamp ``1030:62B1`` (``bp`` = max air X speed). When ``g.input_lr`` is set,
    add a facing-direction accel (``(facing << 4) sar motion_mode``) to Xvel; clamp the result to
    ``[-bp, +bp]``. Returns the new Xvel."""
    p, g, _ = _views(rb, rw)
    ax = 0                                                          # [62B5]
    if g.input_lr != 0:                                         # [62B9]
        ax = (p.facing << 4) & 0xFFFF                              # [62C0-62C9] facing << 4
    ax = _s16(ax) >> p.motion_mode                                 # [62CB-62CF] sar ax, motion_mode
    dx = p.xvel + ax                                               # [62D1-62D5]
    bp = _s16(bp)
    if dx >= bp:                                                   # [62D7]
        return bp & 0xFFFF
    if dx <= -bp:                                                  # [62DB-62DF]
        return (-bp) & 0xFFFF
    return dx & 0xFFFF                                             # [62E1]


def _gravity_y(rw, rb, bp: int) -> int:
    """Gravity + terminal-velocity clamp ``1030:6309`` (``bp`` = terminal). Add ``0x10`` to Yvel (``4`` and a
    lighter terminal ``bp>>3`` when ``g.low_gravity == 1``); clamp up to ``bp``. Returns the new Yvel."""
    p, g, _ = _views(rb, rw)
    dx = p.yvel                                                    # [630C]
    ax = 0x10                                                      # [6310]
    bp = _s16(bp)
    if g.low_gravity == 1:                                         # [6313]
        ax = 4                                                     # [631A]
        bp = bp >> 3                                               # [631D-6321] sar bp,1 x3
    dx += ax                                                       # [6323]
    return (bp if dx >= bp else dx) & 0xFFFF                       # [6325-632B]


def collision_airborne(rw, rb) -> dict:
    """Recover the off-top / in-air physics ``1030:63B5`` (run after the worker when the player is airborne, and
    from the `5B81` off-top path). Applies air drift (`62B1`, ±0x50) + gravity (`6309`, terminal 0xC0), then sets
    the fall animation. Returns the dict of writes. Pure."""
    p, g, be = _views(rb, rw)
    p.xvel = _air_drift_x(rw, rb, 0x50)                           # [63B7-63BA]
    new_yvel = _gravity_y(rw, rb, 0xC0)                            # [63BD-63C0]
    p.yvel = new_yvel
    yvel = _s16(new_yvel)
    if g.glider != 0:                                       # [63C3] (dormant in normal play)
        al = 0x2D if yvel < 0 else 0x2E                           # [63CA-63D3] fly frames
        p.sprite = ((p.facing_lo & 0x80) << 8) | al               # [63F4-63FB]
        return be.writes
    if yvel <= 0:                                                # [63D6] rising / apex -> no anim change
        return be.writes
    g.fall_grace = 6                                              # [63DD]
    if g.anim_gate != 0:                                          # [63E2] gated -> no anim change
        return be.writes
    al = 0x0D if g.fall_frames >= 0x0C else 0x0C                  # [63E9-63F2] fall frames
    p.sprite = ((p.facing_lo & 0x80) << 8) | al                   # [63F4-63FB]
    return be.writes


def _dec_floor8(v: int) -> int:
    """`sub v,1 ; adc v,0` saturating decrement of a byte (clamps at 0)."""
    v &= 0xFF
    return (v - 1) & 0xFF if v != 0 else 0


def _coll_soft_land(p: PlayerView, g: PlayerGlobals, new_y: int) -> None:
    """The soft-land exit `1030:64D9`: zero Yvel + set the grounded state flags."""
    p.yvel = 0                                            # [64D9]
    g.fall_grace = _dec_floor8(g.fall_grace)              # [64DF-64E4] sat-dec
    g.fall_latch = 0                                      # [64E9]
    g.airborne = 2                                        # [64EE]
    g.last_land_y = new_y                                 # [64F3-64F6]


def collision_land(rb, rw, read_es, di: int) -> dict:
    """Recover the player land-on-ground routine ``1030:641F``.

    ``rb``/``rw`` read DS; ``read_es(off)`` reads the map byte ``es:[off]`` (``es=[0x2DDA]``); ``di`` is the
    player's tile pointer (computed in the 5A96 main body). Snaps Y to the tile top, applies the slope offset
    (capped by the descent ``sar(Yvel,4)``), then resolves the landing impact — soft (zero Yvel + grounded
    flags) vs hard (landing dust, camera shake, bounce, land anim). Returns the dict of writes (incl. the
    landing-dust trail-ring writes). Pure."""
    p, g, be = _views(rb, rw)
    p.motion_mode = 0                                                # [641F]
    if p.yvel < 0:                                                   # [6424] rising -> airborne
        g.airborne = collision_fall(g.airborne)                      # [6401]
        return be.writes
    g.low_gravity = 0                                                # [642D]
    y = p.y & 0xFFF0                                                 # [6432] snap Y to tile top
    p.y = y
    new_y = y
    foot = Tables(rb).tile_props[read_es(di & 0xFFFF)]     # [6437-643C] foot tile property
    if foot != 0:                                                   # [6441]
        off = _s16(collision_slope_offset(foot, p.x))               # [6445]
        cap = p.yvel >> 4                                            # [6448] sar Yvel,4
        if cap > 0 and off >= cap:                                  # [6454/6456]
            off = cap
        new_y = (y + off) & 0xFFFF
        p.y = new_y                                                  # [6478]
    else:                                                            # [645E] below tile
        below = Tables(rb).tile_props[read_es((di - 0x100) & 0xFFFF)]
        if below != 0:                                              # [6469]
            off = _s16(collision_slope_offset(below, p.x))           # [646D]
            if off < 0x10:                                          # [6470]
                new_y = (y + ((off - 0x10) & 0xFFFF)) & 0xFFFF
                p.y = new_y                                          # [6475-6478]

    bd2 = g.fall_frames
    if bd2 <= 4:                                                    # [647C] jbe 64D9
        _coll_soft_land(p, g, new_y)
        return be.writes
    trail = player_emit_trail(p.x, new_y, 0, g.trail_ring)           # [6483] 5E18 landing dust (ungated)
    if trail is not None:
        be.writes.update(trail[0])
        g.trail_ring = trail[1]
    yvel = p.yvel
    if (_s16(new_y) - _s16(g.last_land_y)) < 0x20 or yvel < 0x50:    # [6486-6497]
        _coll_soft_land(p, g, new_y)
        return be.writes
    g.last_land_y = new_y                                            # [6499]
    if bd2 >= 0x14 and yvel > 0xA0:                                  # [649F-64AC]
        g.camera_shake = 8                                          # [64AE] camera shake
    if bd2 <= 0x0A:                                                 # [64B3] jbe 64D9
        _coll_soft_land(p, g, new_y)
        return be.writes
    if (g.level_flags & 1) == 0:                                    # [64BA] hard land: bounce unless bit0
        p.yvel = 0xFFE0                                             # [64C1]
    p.sprite = (p.sprite & 0xE000) | 0x000C                          # [64C7] land anim frame 0xC
    g.fall_frames = 0                                               # [64D3]
    return be.writes


def _ceiling_headbump_pushout(rb, rw, read_es) -> dict:
    """[asm 668B] The Yvel==0 head-bump branch — the "push out of a solid ceiling" nudge.

    It pushes the object pointed to by ``g.current_object`` one tile toward the open side. The catch: on the
    PLAYER's own collision that pointer is never populated — it is NULL (0) — so the routine dereferences low
    DGROUP (``[0x0000]``/``[0x0002]``) as a bogus X/Y, lands on a cell far off the map (a 0xFF border tile that
    is never ceiling-solid), and returns without writing. So for the player this is a latent **no-op / dead
    code** in the original game (the intended un-stick never actually runs). Transcribed LITERALLY rather than
    hardcoded to ``{}`` so the null-pointer computation stays byte-exact vs the VM even in the theoretical
    state where the bogus cell *is* ceiling-solid (then it writes ``[[ptr]] += dx`` exactly as the ASM does —
    a genuinely DYNAMIC offset, which is why the raw ``rw(ptr)`` deref stays). anim in ``{0x0A, 0x15}`` skips
    it outright. Pure."""
    p, g, _ = _views(rb, rw)
    if (p.sprite & 0xFF) in (0x0A, 0x15):                        # [668E/6692] two anim states -> no push-out
        return {}
    ptr = g.current_object                                       # [6698] di = current object (NULL on the player path)
    col = (_s16(rw(ptr)) >> 4) & 0xFF                            # [66A5-66A9] sar (X word),4
    row = (_s16(rw((ptr + 2) & 0xFFFF)) >> 4) & 0xFF            # [669E-66A3] sar (Y word),4
    cell = ((row << 8) | col) & 0xFFFF                           # [66AB] di = (row<<8)|col
    if not (Tables(rb).ceil_props[read_es(cell)] & 1):   # [66B4-66BA] this cell not solid -> nothing
        return {}
    dx = 0x10                                                   # [66BC] default: push right one tile
    if not (Tables(rb).ceil_props[read_es((cell - 1) & 0xFFFF)] & 1):  # [66BF-66C6] left tile open?
        dx = -0x10                                              # [66C8] neg -> push toward the open (left) side
    return {ptr: (rw(ptr) + dx) & 0xFFFF}                       # [66CA-66CE] add [ [ptr] ], dx


def collision_ceiling(rb, rw, read_es, di: int) -> dict:
    """Recover the player ceiling (head-bump) collision ``1030:5C16..5C76`` (part of 5B81, runs when the player
    is rising into the tile above). ``read_es(off)`` reads map byte ``es:[off]``; ``di`` is the tile-above
    pointer (the player's tile minus one row); ``rb``/``rw`` read DS.

    Reads the tile above + the player's tile, dispatches the **ceiling-tile handler** (`cs:[0x7DA9]`) indexed by
    ``0x805E[tile_above] & 0xF``: idx 0 = no-op (`0x6672`); idx 1 = head-bump (`0x6673`: zero Yvel + snap Y down
    below the ceiling); idx 2 = hazard ceiling (`0x65AF` -> `0x65B3` = the off-camera death/respawn trigger, the
    same routine the ground idx6 dispatches to). Then, if the player's tile is ceiling-solid and Y>0, a sideways
    corner-slip nudge. idx 3-15 are unwitnessed and fail loud. Returns the dict of writes. Pure."""
    p, g, be = _views(rb, rw)
    tile_above = read_es(di & 0xFFFF)                            # [5C18]
    player_tile = read_es((di + 0x100) & 0xFFFF)                 # [5C1B]
    solid = rb((TILE_CEIL_SOLID_TABLE + player_tile) & 0xFFFF) & 1   # [5C20-5C24] ah = 0x7E5E[player_tile]
    idx = rb((TILE_CEIL_HANDLER_TABLE + tile_above) & 0xFFFF) & 0x0F  # [5C26-5C2A] cs:[0x7DA9] index

    if idx == 1:                                                # [6673] head-bump
        if p.yvel != 0:                                         # [6678] rising -> zero Yvel + snap below ceiling
            p.yvel = 0                                          # [667A]
            p.y = ((p.y & 0xFFF0) + 0x10) & 0xFFFF               # [6680-6685]
        else:                                                   # [668B] Yvel==0: the "push out of a solid ceiling"
            be.writes.update(_ceiling_headbump_pushout(rb, rw, read_es))  # branch (a null-pointer no-op for the player)
    elif idx == 2:                                              # [65AF -> 65B3] hazard ceiling: off-camera death trigger
        be.writes.update(_offcamera_trigger(rb))                # (the SAME routine the ground idx6 dispatches to)
    elif idx != 0:                                              # idx 3-15 still unwitnessed
        raise NotImplementedError(f"ceiling handler idx {idx} not recovered")

    if solid and _s16(be.writes.get(PLAYER_Y, p.y)) > 0:           # [5C38-5C42] solid + Y>0 -> corner-slip nudge
        dx = -1 if p.xvel > 0 else 1                             # [5C44-5C51] step away from the facing edge
        n1 = Tables(rb).ceil_props[read_es((di + dx + 0x100) & 0xFFFF)]  # [5C54-5C5C]
        if n1 == 0:                                              # [5C5E] this side is open -> slip into it
            p.x = (p.x + dx * 2) & 0xFFFF                         # [5C70-5C72]
        else:                                                    # [5C60-5C64] other side
            n2 = Tables(rb).ceil_props[read_es((di - dx + 0x100) & 0xFFFF)]  # [5C66-5C6B]
            if n2 == 0:                                          # [5C6E] only the far side is open
                p.x = (p.x - dx * 2) & 0xFFFF                     # [5C70-5C72] (dx negated)
            # else: wedged between two solid tiles -> no nudge
    return be.writes


def _ground_snap_or_fall(rb, rw, read_es, di: int) -> dict:
    """Ground handler 0 ``1030:65EF`` (the most common): if at rest (Yvel==0) and a reachable solid/slope tile
    sits a row below, step the player down one row and land on it; otherwise mark airborne (`0x6401`). This is
    what keeps the caveman stuck to descending ground instead of floating off the lip of a step."""
    p, g, be = _views(rb, rw)
    if p.yvel != 0:                                                 # [65EF] still moving vertically -> fall
        g.airborne = collision_fall(g.airborne)
        return be.writes
    al = read_es((di + 0x100) & 0xFFFF)                             # [65F6] tile one row below the foot tile
    prop = Tables(rb).tile_props[al]                      # [65FB-65FE]
    if prop == 0:                                                   # [65FF] empty below -> fall
        g.airborne = collision_fall(g.airborne)
        return be.writes
    off = _s16(collision_slope_offset(prop, p.x))                   # [6603]
    if off >= 0x10:                                                # [6606-6609] too far down -> fall
        g.airborne = collision_fall(g.airborne)
        return be.writes
    stepped_y = (p.y + 0x10) & 0xFFFF                                # [660F] drop one tile row
    rw2 = lambda o: stepped_y if o == PLAYER_Y else rw(o)             # noqa: E731 — the stepped-Y read shim
    out = collision_land(rb, rw2, read_es, (di + 0x100) & 0xFFFF)   # [660B/6614 -> 0x641F] land on the row below
    out.setdefault(PLAYER_Y, stepped_y)
    return out


def collision_ground_handler(idx: int, rb, rw, read_es, di: int) -> dict:
    """Recover the ground tile-handler dispatch ``cs:[0x7D9B]`` (`5C04`, ``bx = 0x7F5E[tile_below]*2``). ``di`` is
    the foot-tile pointer (player tile + one row); the handlers are thin compositions over the verified
    land/fall/slope cores. Returns the dict of writes. Pure.

    idx 0 `65EF` snap-down-or-fall; 1 `6641` land; 2/3/4 `6657/6660/6669` land + slope-shift ``motion_mode=idx-1``;
    5 `6645` ``motion_mode=0`` then conditional fall(``drop_gate``)/land; 6 `65AF` special level trigger
    (unwitnessed, fail loud); 7 `6672` no-op."""
    if idx == 0:                                                   # [65EF]
        return _ground_snap_or_fall(rb, rw, read_es, di)
    if idx == 1:                                                   # [6641] plain land
        return collision_land(rb, rw, read_es, di)
    if idx in (2, 3, 4):                                           # [6657/6660/6669] land + slope shift
        out = collision_land(rb, rw, read_es, di)
        p, _g, _be = _views(rb, rw, out)
        p.motion_mode = idx - 1                                     # 2->1, 3->2, 4->3
        return out
    if idx == 5:                                                   # [6645] conditional land/fall
        p, g, be = _views(rb, rw)
        if g.drop_gate != 0:                                       # [664A] blocked -> fall
            p.motion_mode = 0
            g.airborne = collision_fall(g.airborne)
            return be.writes
        out = collision_land(rb, rw, read_es, di)                  # [6651 -> 0x641F]
        p2, _g2, _be2 = _views(rb, rw, out)
        p2.motion_mode = 0                                         # [6645] (641F also zeroes it)
        return out
    if idx == 6:                                                   # [65AF] off-camera / special trigger
        return _offcamera_trigger(rb)
    if idx == 7:                                                   # [6672] no-op
        return {}
    raise NotImplementedError(f"ground handler idx {idx} not recovered")


def _bridge_dirty(new_tile: int, off: int, g: PlayerGlobals, redraws: list, rb) -> None:
    """The bridge sag/spring dirty step ``1030:5C7B`` (`bx = new_tile`, the tile at map offset `off` = the foot
    `di`). For ``[0x4DF8+tile] >= 1`` mark the whole grid dirty. For ``== 0`` the ASM redraws that ONE tile
    directly (`5C8E -> 653D`): if the cell is on-screen (col in ``[cam_col, cam_col+0x14)``, row in
    ``[cam_row, cam_row+0x0C)``) queue an on-page re-blit of `off` — the same shared 3B77 tile blit the
    bonus-collect redraw uses, reading the tile's new frame from the live map — and set the page-dirty flag;
    an off-screen cell does nothing (the 653D `stc` early-out)."""
    if rb((DIRTY_KIND_TABLE + new_tile) & 0xFFFF) >= 1:            # [5C7B-5C80] jb 5c8e
        g.grid_dirty = 1                                            # [5C82]
        g.grid_dirty_token = 0x55AA                                 # [5C87]
        return
    col, row = off & 0xFF, (off >> 8) & 0xFF                       # [6546-6558] 653D on-screen test vs camera
    if ((col - g.cam_col) & 0xFF) < 0x14 and ((row - g.cam_row) & 0xFF) < 0x0C:
        redraws.append(off & 0xFFFF)                               # [6599 -> 3B77] re-blit the one tile
        g.page_dirty = 1                                           # [659C]


def collision_bridge_dip(di: int, read_es, rw, rb) -> tuple:
    """Recover the bridge/platform sag-under-weight ``1030:5BB8..5C01`` (runs when the foot tile differs from the
    one currently dipping, ``dipping_tile != di``). ``read_es(off)`` reads the live tile map; ``di`` is the
    foot-tile pointer. Returns ``(ds_writes, map_writes, redraws)`` where ``map_writes`` maps an es-relative tile
    offset to its new byte and ``redraws`` lists the (type-0) tile offsets to re-blit on-page. Pure (no live
    writes).

    First spring the previously-dipping tile back up — walk its graphic id **down** one frame at a time while
    the tile is still a sag frame (`0x805E[id-1] & 0x20`), writing+dirtying each, until it clears, then reset
    ``dipping_tile = 0x55AA``. Then, if the new foot tile is a bridge frame (`0x805E[id] & 0x20`), dip it
    **down** (graphic id+1), mark it as the dipping tile, and dirty it."""
    _p, g, be = _views(rb, rw)
    map_w: dict = {}
    redraws: list = []
    bab = g.dipping_tile
    if bab != 0x55AA:                                              # [5BBB] something is currently dipping
        sdi = bab & 0xFFFF
        cur = read_es(sdi)                                         # [5BC8]
        while True:
            cur = (cur - 1) & 0xFF                                 # [5BCD] dec to the previous sag frame
            if not (rb((BRIDGE_FLAG_TABLE + cur) & 0xFFFF) & 0x20):  # [5BCE-5BD3]
                g.dipping_tile = 0x55AA                             # [5BD5] sprung fully back -> none dipping
                break
            map_w[sdi] = cur                                       # [5BDE-5BE0] es:[di] = id-1
            _bridge_dirty(cur, sdi, g, redraws, rb)               # [5BE3]
    cur = read_es(di & 0xFFFF)                                     # [5BE8] the new foot tile
    if rb((BRIDGE_FLAG_TABLE + cur) & 0xFFFF) & 0x20:             # [5BEB-5BF2] is it a bridge frame?
        g.dipping_tile = di & 0xFFFF                               # [5BF4] now dipping
        nw = (cur + 1) & 0xFF                                      # [5BF8] inc -> next sag frame
        map_w[di & 0xFFFF] = nw                                    # [5BFB] es:[di] = id+1
        _bridge_dirty(nw, di & 0xFFFF, g, redraws, rb)            # [5BFE]
    return be.writes, map_w, redraws


def _wall_marker_push(rw) -> dict:
    """The wall-impact marker registration ``1030:64FA``: drop ``(X<<3, Y<<3)`` into the first free marker
    slot (free = leading word ``0x55AA``). Returns the slot's word/byte writes, or ``{}`` if the list is
    full. NOTE: never reached in any current demo (the side-solid ``0x805E&0x10`` tile never occurs; walls
    block via ``collision_hblock``) — transcribed from the ASM at ASM_MATCHED confidence, not lockstep
    VERIFIED."""
    be = DictBackend(lambda o: rw(o) & 0xFF, rw)
    p, g = PlayerView(be), PlayerGlobals(be)
    for marker in g.wall_markers:                                  # [64FD-6529] the 64FA slot scan
        if marker.free:                                            # [64FD] leading word == 0x55AA
            marker.x = (p.x << 3) & 0xFFFF                         # [6505-650A] X<<3 (word)
            marker.y = (p.y << 3) & 0xFFFF                         # [650C-6511] Y<<3 (word)
            marker.b4 = 0                                          # [6514]
            marker.b5 = 0                                          # [6518]
            marker.b7 = 0                                          # [651C]
            return be.writes
    return {}                                                      # [6522/6529] list full


def collision_side_handler(idx: int, read_es, rw, rb, di: int) -> dict:
    """Recover the horizontal/body side-collision dispatch ``cs:[0x7D95]`` (`5C92`/`5CAC`, ``bx = 0x7E5E[tile]*2``)
    run for each tile cell along the player's vertical extent. ``di`` is the scanned cell pointer. Returns the dict
    of writes (DS scalars and/or `0x6EA9` wall-marker slots). Pure.

    idx 0 `652C`: if the tile is **side-solid** (`0x805E[tile] & 0x10`), register a wall-impact marker (`64FA`);
    idx 1 `6539`: wall block = `collision_hblock` (undo the X step + stop); idx 2 `65AF` trigger (unwitnessed,
    fail loud). idx 3-8 reuse the ground handlers `65EF/6641/6657/6660/6669/6645` (unwitnessed in the side scan)."""
    if idx == 0:                                                   # [652C]
        tile = read_es(di & 0xFFFF)
        if rb((SIDE_FLAG_TABLE + tile) & 0xFFFF) & 0x10:           # [6531] side-solid -> wall marker
            return _wall_marker_push(rw)                           # [6536 -> 64FA]
        return {}                                                  # [6538] not solid
    if idx == 1:                                                   # [6539] wall block
        p, _g, be = _views(rb, rw)
        new_x, new_xvel = collision_hblock(p.x, p.xvel)             # [6407]
        p.x = new_x
        p.xvel = new_xvel
        return be.writes
    if idx == 2:                                                   # [65AF -> 65B3] hazard tile (e.g. head-spike):
        return _offcamera_trigger(rb)                             # the off-camera death/respawn trigger, the
        #                                                           SAME routine ceiling idx2 / ground idx6 use
    if idx in (3, 4, 5, 6, 7, 8):                                  # ground handlers, unwitnessed in side scan
        raise NotImplementedError(f"side scan ground handler idx {idx} (0x7D95) not witnessed")
    raise NotImplementedError(f"side handler idx {idx} (0x7D95) not recovered")  # idx >= 9


class _Overlay:
    """Byte-level read-through overlay used by ``collision`` so a read later in the routine observes a write made
    earlier (the ASM mutates memory in place; the pure handlers return write dicts). DS writes are split into
    bytes using ``COLLISION_BYTE_FIELDS`` for widths; map (es) writes are byte-keyed. ``ds``/``mp`` accumulate the
    net byte writes = the routine's write-contract. Also a dgroup-view backend (``_IS_DGROUP_BACKEND``): the
    composition binds :class:`PlayerView`/:class:`PlayerGlobals` straight onto it, so named reads see earlier
    writes and named writes land in the same byte contract (a word field writes its two bytes, exactly like
    ``apply_ds`` splitting a non-byte-field word)."""

    _IS_DGROUP_BACKEND = True

    def __init__(self, rb, rw, read_es):
        self._rb, self._read_es = rb, read_es
        self.ds: dict = {}
        self.mp: dict = {}
        self.redraws: list = []   # map offsets the bridge-dip queued for a direct on-page tile re-blit (653D)

    def rb(self, a: int) -> int:
        a &= 0xFFFF
        return (self.ds[a] if a in self.ds else self._rb(a)) & 0xFF

    def rw(self, a: int) -> int:
        a &= 0xFFFF
        return self.rb(a) | (self.rb((a + 1) & 0xFFFF) << 8)

    def wb(self, a: int, v: int) -> None:
        self.ds[a & 0xFFFF] = v & 0xFF

    def ww(self, a: int, v: int) -> None:
        self.ds[a & 0xFFFF] = v & 0xFF
        self.ds[(a + 1) & 0xFFFF] = (v >> 8) & 0xFF

    def read_es(self, o: int) -> int:
        o &= 0xFFFF
        return (self.mp[o] if o in self.mp else self._read_es(o)) & 0xFF

    def apply_ds(self, writes: dict) -> None:
        for a, v in writes.items():
            a &= 0xFFFF
            self.ds[a] = v & 0xFF
            if a not in COLLISION_BYTE_FIELDS:
                self.ds[(a + 1) & 0xFFFF] = (v >> 8) & 0xFF

    def apply_map(self, writes: dict) -> None:
        for o, v in writes.items():
            self.mp[o & 0xFFFF] = v & 0xFF


def _offcamera_trigger(rb) -> dict:
    """The off-camera death/respawn trigger ``1030:65B3`` (called as `65AF`). If not already triggered
    (``respawn_state == 0``): consume a life, reset the energy, arm the respawn (``respawn_state = 2``); if no
    lives remain, set the game-over flag (``end_signal = 1``). Returns the dict of writes. Pure."""
    be = DictBackend(rb, lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8))
    g = PlayerGlobals(be)
    if g.respawn_state != 0:                                       # [65B3] already triggered
        return be.writes
    if g.lives == 0:                                              # [65BA] no lives left -> game over
        g.end_signal = 1                                          # [65D0]
        return be.writes
    g.lives = g.lives - 1                                          # [65C1]
    g.energy = 0                                                   # [65C5]
    g.respawn_state = 2                                            # [65C9]
    return be.writes


def _collision_worker(ov: _Overlay, cell_bx: int) -> None:
    """The tile-interaction worker ``1030:5B81`` composed onto the overlay ``ov`` (``cell_bx`` = the tile one row
    above the foot). Off-top (`Y<=-1`) + foot-tile remap + bridge-dip + ground dispatch + ceiling."""
    p, g = PlayerView(ov), PlayerGlobals(ov)
    if _s16(p.y) <= -1:                                            # [5B84 cmp Y,-1 / 5B89 jg 5B96]
        # Off-top: the player is above the level ceiling (e.g. a high bounce off a spider). There is no tile
        # up here, so the worker skips ALL tile interaction and just runs the in-air physics, marks the player
        # firmly airborne, and returns. Transcribed from the 5B8B branch (disasm):
        #   5B8B  call 63B5            -> collision_airborne (air drift + gravity + fall anim; already recovered)
        #   5B8E  mov byte [6BF3],FFh  -> airborne flag = 0xFF (a full byte, not the |1 used elsewhere)
        #   5B93  jmp 5C77             -> the worker epilogue (pop bp/bx/ax; ret) = skip the tile body
        ov.apply_ds(collision_airborne(ov.rw, ov.rb))             # [5B8B -> 63B5]
        g.airborne = 0xFF                                          # [5B8E]
        return                                                     # [5B93]
    di = (cell_bx + 0x100) & 0xFFFF                                # [5B97] foot tile
    foot_tile = ov.read_es(di) if g.map_rows > (di >> 8) else 0    # [5B9D-5BA6] map-bounds clamp
    idx = Tables(ov.rb).floor_props[foot_tile]         # [5BA8] cs:[0x7D9B] index
    if g.dipping_tile != di:                                       # [5BB2] not already dipping here -> bridge-dip
        bds, bmp, bredraws = collision_bridge_dip(di, ov.read_es, ov.rw, ov.rb)  # [5BB8]
        ov.apply_ds(bds)
        ov.apply_map(bmp)
        ov.redraws.extend(bredraws)
    ov.apply_ds(collision_ground_handler(idx, ov.rb, ov.rw, ov.read_es, di))   # [5C04]
    cbx = cell_bx - 0x100                                          # [5C09]
    if cbx >= 0 and p.yvel <= 0:                                   # [5C0D jb / 5C0F jg] in-bounds + not falling
        ov.apply_ds(collision_ceiling(ov.rb, ov.rw, ov.read_es, cbx & 0xFFFF))  # [5C16]


def _side_scan(ov: _Overlay, cell: int, conditional: bool) -> None:
    """One vertical scan-loop cell ``5C92`` (first, unconditional) / ``5CAC`` (rest, only tile types 2 & 4)."""
    tile = ov.read_es(cell & 0xFFFF)                              # [5C97 / 5CB1]
    idx = Tables(ov.rb).ceil_props[tile]          # [5C9A / 5CB4] remap 0x7E5E
    if conditional and idx not in (2, 4):                         # [5CB8-5CBE] 5CAC dispatch filter
        return
    ov.apply_ds(collision_side_handler(idx, ov.read_es, ov.rw, ov.rb, cell & 0xFFFF))


def collision(rb, rw, read_es) -> tuple:
    """Recover the full player ground/tile collision ``1030:5A96`` (called from the player update at `5A41` after
    the Y integrate). ``rb``/``rw`` read DS; ``read_es(off)`` reads the live tile map (``es=[0x2DDA]``). Returns
    ``(ds_writes, map_writes, redraws)`` — the byte-keyed DS + map write-contract plus the list of tile offsets a
    bridge-dip queued for a direct on-page re-blit (the 653D render side-effect). Pure (no live writes/blits).

    Computes the player's tile cell from X/Y, range-checks vs the camera, runs the tile-interaction worker
    (`5B81`: bridge-dip + ground dispatch + ceiling), resolves the post-worker fall/land state, then scans the
    player's vertical extent for horizontal/body collisions (`cs:[0x7D95]`)."""
    ov = _Overlay(rb, rw, read_es)
    p, g = PlayerView(ov), PlayerGlobals(ov)

    # --- tile cell + scan parameters [5A99-5AC4] ---
    row_m1 = ((_s16(p.y) >> 4) - 1) & 0xFFFF                       # (Y>>4) - 1
    cell_high = ((row_m1 & 0xFF) << 8) | ((row_m1 >> 8) & 0xFF)    # [5AA4] xchg al,ah
    cell_bx = ((_s16(p.x) >> 4) + cell_high) & 0xFFFF              # [5AC5-5ACB] X>>4 + (row<<8)
    anim_idx = ((p.sprite & 0x1FFF) << 1) & 0xFFFF                 # [5AA6-5AAD] anim frame *2
    dh = Tables(ov.rb).player_anim_height[anim_idx]     # [5AAF] player vertical extent
    xvel = p.xvel
    x_edge = 9 if xvel > 0 else (-9 if xvel < 0 else 0)            # [5AB3-5AC1] leading-edge X offset

    # --- camera range check [5ACD-5B16] ---
    if _out_of_camera_range(p, g):                                # writes only on the out-of-range branch
        if g.level == 0x0E:                                       # [5B18]
            g.end_signal = 0xFF                                   # [5B1F] the final level: falling out = THE END
        else:
            ov.apply_ds(_offcamera_trigger(ov.rb))               # [5B26 -> 65B3]

    g.airborne = 0                                                # [5B29] clear the airborne flag
    _collision_worker(ov, cell_bx)                               # [5B2E]

    # --- post-worker fall / land [5B31-5B54] ---
    if g.airborne == 1:                                           # [5B31] airborne after the worker
        if g.unk_6BFE == 0:                                       # [5B38]
            ov.apply_ds(collision_airborne(ov.rw, ov.rb))        # [5B44 -> 63B5]
            if p.yvel > 0:                                        # [5B47] still descending
                g.fall_frames = g.fall_frames + 1                 # [5B4E]
        else:                                                    # [5B3F -> 64DF] soft-land tail
            g.fall_grace = _dec_floor8(g.fall_grace)
            g.fall_latch = 0
            g.airborne = 2
            g.last_land_y = p.y
            g.fall_frames = 0
    else:
        g.fall_frames = 0                                         # [5B54]

    # --- vertical side-collision scan [5B5B-5B7B] ---
    if _s16(p.y) > 0:                                             # [5B5B]
        bx = ((_s16(p.x) + x_edge) >> 4) & 0xFFFF                  # [5B62-5B66]
        bx = (bx + cell_high) & 0xFFFF                            # [5B68]
        _side_scan(ov, bx, conditional=False)                    # [5B6A] first cell
        dh_left = dh
        while True:
            if bx < 0x100:                                       # [5B72-5B76] sub bx,0x100 ; jb
                break
            bx = (bx - 0x100) & 0xFFFF
            dh_left = (dh_left - 0x10) & 0xFF                     # [5B78]
            if dh_left == 0 or dh_left > 0x80:                   # [5B7B] ja (unsigned >0 after sub means no borrow)
                break
            _side_scan(ov, bx, conditional=True)                 # [5B6F] subsequent cells

    return ov.ds, ov.mp, ov.redraws


def _out_of_camera_range(p: PlayerView, g: PlayerGlobals) -> bool:
    """The camera visibility test ``1030:5ACD-5B16`` — True when the player is off the visible map (the only
    branch with side effects). Pure."""
    if abs((_s16(p.y) >> 4) - _s16(g.cam_row_word)) > 0x0B:       # [5ACD-5ADD]
        return True
    if abs((_s16(p.x) >> 4) - _s16(g.cam_col_word)) > 0x14:       # [5ADF-5AEF]
        return True
    if (g.level_flags & 4) and _s16(p.y) < ((_s16(g.cam_row_word) << 4) & 0xFFFF):  # [5AF1-5B01]
        return True
    if _s16(p.y) < 0:                                             # [5B03] above the top -> in range (no trigger)
        return False
    return _s16(p.y) > (((g.map_rows + 1) << 4) & 0xFFFF)         # [5B0A-5B16] below the map bottom
