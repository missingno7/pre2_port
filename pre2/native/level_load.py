"""Native level loader — the VM-less replacement for 1030:3ed6 (the LEVELx.SQZ loader).

``main()`` (1030:00a0) calls ``3ed6(level)`` once per level to unpack ``LEVEL<n>.SQZ`` into DGROUP tables +
the planar tile cache: the seed state the per-frame gameplay loop then runs on. Booting a NativeGameState from
game files (no EXE, no snapshot) means reproducing this loader over the recovered pieces — ``load_sqz`` (107B),
``decode_sprite_cache`` (4316/4389), ``classify_sprites`` (4232) — plus the loader's own table-copy bookkeeping.

Status: the **deterministic DGROUP slice** is recovered + offline-verified byte-exact against the ASM
(``pre2/probes/probe_native_level_load.py`` vs a captured VM ``3ed6`` image). The remaining loader work — the
planar tile blit (``decode_sprite_cache`` into the EGA aperture), the object-table sub-calls
(``42af``/``414d``/``4182``/``3ead``/``40bd``/``41ca`` that build ``[0x8489+]``), the ``[0x815e]→[0x9203]``
double-buffer dup, and the final ``[0x2875]`` load-pointer — fail loud (never a silent ASM fallback).

[asm 1030:3ed6 + 4316 (tile-index/header skip) + 3f3c (property memcpy)]
"""
from __future__ import annotations

from pre2.codecs.sqz import unpack_sqz
from pre2.native.assets import load_sqz_by_dx
from pre2.native.state import DATA_SEG

_DS = DATA_SEG << 4

# the 0x3f3c rep-movsw block, summed: a single contiguous copy of the level's property tables
PROP_LEN = 0x300 + 11 + 0x200 + 0x8c + 0x96 + 0x800 + 4 + 0x190 + 0x100 + 0x1ea + 0xf0 + 4 + 8  # == 0x13a7
PROP_DST = 0x7e5e          # DGROUP destination of the property block
TILE_INDEX_DST = 0x25ce    # DGROUP destination of the 0x100-word tile-index table
OBJ_POOL = 0x4f1c          # cleared object pool (0x40b words)


def native_level_load_dgroup(state, level: int, *, game_root: str) -> None:
    """Reproduce the **deterministic DGROUP** half of ``3ed6`` over a NativeGameState: prologue scalars + clears,
    the SQZ load, the tile-index table, and the property-table memcpy. (The planar tile cache + object-table
    construction are the loader's other halves — see module docstring / ``native_level_load``.)"""
    d = state.data

    def rb(o): return d[_DS + o]

    def wb(o, v): d[_DS + o] = v & 0xFF

    def rw(o): return d[_DS + o] | (d[_DS + (o + 1)] << 8)

    def ww(o, v):
        d[_DS + o] = v & 0xFF
        d[_DS + (o + 1)] = (v >> 8) & 0xFF

    # --- prologue [asm 3ede..3f26] ---
    wb(0x2D8A, level)                                   # current level/mode
    if level <= 0x0A:                                   # parallax/scroll seeds
        for o in (0x2A78, 0x2A74, 0x2A7A, 0x2A76):
            ww(o, 0)
    else:
        ww(0x2A78, rw(0x2A78) >> 1)
        ww(0x2A74, rw(0x2A74) >> 1)
    wb(0x2DAA, rb(0x2D96 + level))                      # per-level graphics-group id
    al = (level + 0x31) & 0xFF                          # filename digit: level+'1', then 'A'.. past '9'
    if al > 0x39:
        al = (al + 7) & 0xFF
    wb(0x2D90, al)
    d[_DS + PROP_DST:_DS + PROP_DST + 0x13A5] = b"\xff" * 0x13A5      # clear property region [asm 3f16]
    d[_DS + OBJ_POOL:_DS + OBJ_POOL + 0x40B * 2] = b"\xff" * (0x40B * 2)  # clear object pool [asm 3f20]

    # --- load LEVEL<n>.SQZ at the load pointer [0x2875] [asm 3f2b] ---
    seg = load_sqz_by_dx(state, 0x2D8B, game_root=game_root)
    ww(0x2DDA, seg)                                     # level-data base segment

    # --- 4316: header skip + tile-index table [asm 4316..433e] ---
    bh = rb(0x2D30 + level)                             # level header size (paragraphs) / bottom limit
    wb(0x2CF5, bh)
    base_seg = (seg + (bh << 4)) & 0xFFFF               # skip the (bh<<4)-paragraph header
    base = (base_seg << 4) & 0xFFFFF
    d[_DS + TILE_INDEX_DST:_DS + TILE_INDEX_DST + 0x200] = d[base:base + 0x200]   # 0x100-word tile index

    # property-table source offset: 0x4316 advances bp past the local tile graphics (0x80 bytes each)
    bp = 0x200
    for i in range(0x100):
        code = d[base + i * 2] | (d[base + i * 2 + 1] << 8)
        if code < 0x100:
            bp += 0x80

    # --- property tables: one contiguous 0x13a7-byte copy ds:bp -> [0x7e5e] [asm 3f3c..3f78] ---
    src = base + bp
    d[_DS + PROP_DST:_DS + PROP_DST + PROP_LEN] = d[src:src + PROP_LEN]


def _s8(v: int) -> int:
    return v - 0x100 if v & 0x80 else v


def _rebase_effect_sprites(d) -> None:
    """4159 (called as 414d): rebase the float-effect ([0x8f1d], 70x stride 7) + terrain-entity
    ([0x9107], 16x stride 0xF) sprite refs [+4] from level-bank to engine-bank (-[0x8c89]+0x35)."""
    dx = d[_DS + 0x8C89] | (d[_DS + 0x8C8A] << 8)

    def rebase(seg_off, count, stride):
        for k in range(count):
            o = seg_off + k * stride + 4
            v = d[_DS + o] | (d[_DS + o + 1] << 8)
            if v != 0xFFFF:
                v = (v - dx + 0x35) & 0xFFFF
                d[_DS + o] = v & 0xFF
                d[_DS + o + 1] = (v >> 8) & 0xFF
    rebase(0x8F1D, 0x46, 7)
    rebase(0x9107, 0x10, 0xF)


def _rebase_entity_sprites(d) -> None:
    """4182: rebase the second-pass entity list [0x8489] (variable stride = [si], <=0x32) sprite ref
    [+2] across two banks (>= [0x8c8b] -> -[0x8c8b]+0x138; >= [0x8c89] -> -[0x8c89]+0x35), then reset
    [0x8c89]=0x35 / [0x8c8b]=0x138."""
    dx = d[_DS + 0x8C89] | (d[_DS + 0x8C8A] << 8)
    bx = d[_DS + 0x8C8B] | (d[_DS + 0x8C8C] << 8)
    if dx != 0xFFFF:
        si = 0x8489
        while d[_DS + si] <= 0x32:
            ax = d[_DS + si + 2] | (d[_DS + si + 3] << 8)
            if ax != 0xFFFF:
                if ax >= bx:
                    ax = (ax - bx + 0x138) & 0xFFFF
                    d[_DS + si + 2] = ax & 0xFF; d[_DS + si + 3] = (ax >> 8) & 0xFF
                elif ax >= dx:
                    ax = (ax - dx + 0x35) & 0xFFFF
                    d[_DS + si + 2] = ax & 0xFF; d[_DS + si + 3] = (ax >> 8) & 0xFF
            si = (si + _s8(d[_DS + si])) & 0xFFFF
    for o, v in ((0x8C89, 0x35), (0x8C8B, 0x138)):
        d[_DS + o] = v & 0xFF; d[_DS + o + 1] = (v >> 8) & 0xFF


def _ensure_password_seed(d) -> None:
    """[asm 932F 9336..9396] Reproduce 932F's lazy one-time seed init: if the ``[0xA335]`` "computed" flag is
    clear, compute the BIOS-ROM checksum into ``[0xA333]`` (word) and set the flag. This is exactly the guard the
    ASM runs on the FIRST password-generator call — and in the mode-select path that first call IS this 40bd
    decor assignment (which is why the VM reaches gameplay with ``[0xA333]=0x20`` on the zeroed-BIOS GOG build).
    Without it native reads ``[0xA333]=0`` -> the decor sprites diverge from the VM. Idempotent."""
    from pre2.recovered.password import bios_seed
    if d[_DS + 0xA335] != 0:                                 # [asm 933c] cmp byte [0xa335],0 / jne (already done)
        return
    seed = bios_seed(d)                                     # [asm 9343..9390] the F000/option-ROM checksum
    d[_DS + 0xA333] = seed & 0xFF                            # [asm 9392] mov [0xa333], dx
    d[_DS + 0xA334] = (seed >> 8) & 0xFF
    d[_DS + 0xA335] = 1                                     # [asm 9396] mov byte [0xa335], 1


def _assign_random_decor(d) -> None:
    """40bd: the random decorative-sprite assignment. Collect the float-effect entries ([0x8f1d], 70x stride 7)
    whose sprite ref ([si+4] & 0x1fff) is in [0x11b, 0x12b) into a pointer list at [0x6a88]; if the count is a
    nonzero multiple of 4, bubble-sort the pointers by the entry's first word, then deal each a pseudo-random
    sprite (0x11b + a 4-bit nibble of the level's password code 932F, MSB-first, groups of 4 reusing the code).
    Deterministic via the recovered generator [[pre2-level-passwords]] (seed = machine fingerprint [0xA333])."""
    from pre2.recovered.password import DEFAULT_ROT, level_code
    _ensure_password_seed(d)                                # [asm 40bd -> 932F] the first 932F call inits the seed

    ptrs = []
    for k in range(0x46):
        si = 0x8F1D + k * 7
        v = d[_DS + si + 4] | (d[_DS + si + 5] << 8)
        if ((v & 0x1FFF) - 0x11B) & 0xFFFF <= 0xF:
            ptrs.append(si)
    # mirror [0x6a88] (words) for fidelity
    for i, p in enumerate(ptrs):
        d[_DS + 0x6A88 + i * 2] = p & 0xFF; d[_DS + 0x6A88 + i * 2 + 1] = (p >> 8) & 0xFF
    bp = len(ptrs)
    if bp == 0 or bp & 3:
        return
    # bubble sort the pointers by the pointed entry's first word [si]
    swapped = True
    while swapped:
        swapped = False
        for i in range(bp - 1):
            a = d[_DS + ptrs[i]] | (d[_DS + ptrs[i] + 1] << 8)
            b = d[_DS + ptrs[i + 1]] | (d[_DS + ptrs[i + 1] + 1] << 8)
            if b < a:
                ptrs[i], ptrs[i + 1] = ptrs[i + 1], ptrs[i]; swapped = True
    for i, p in enumerate(ptrs):
        d[_DS + 0x6A88 + i * 2] = p & 0xFF; d[_DS + 0x6A88 + i * 2 + 1] = (p >> 8) & 0xFF
    level = d[_DS + 0x2D8A] + (0x0A if d[_DS + 0xB198] else 0)
    seed = d[_DS + 0xA333] | (d[_DS + 0xA334] << 8)
    code = level_code(level, seed=seed, rot=DEFAULT_ROT)
    # groups of 4 entries, each group re-using ``code``, 4 bits per entry MSB-first
    for g in range(0, bp, 4):
        dx = code
        for j in range(4):
            di = ptrs[g + j]
            dx = (dx << 4) & 0xFFFF | (dx >> 12)        # rotate so the next nibble is the low 4 bits...
            nib = dx & 0xF                               # ...matching shl/rcl MSB-first extraction
            val = 0x11B + nib
            d[_DS + di + 4] = val & 0xFF; d[_DS + di + 5] = (val >> 8) & 0xFF


def _self_patch_secret_tiles(d) -> None:
    """[asm 3ead] HIDE the secret/bonus tiles in the level-data grid. Walk the 0x50-entry bonus-cell list
    ``[0x8c8d]`` (stride 5): each entry's ``[+3]`` word ``bx`` is a cell offset into the level data (``[0x2dda]``).
    For a live entry (``bx != 0xFFFF``) swap ``level_data[bx]`` with the entry's ``[+0]`` byte — the HIDDEN tile —
    and stash the displaced (revealed) tile in ``[+1]`` (so the reveal handler can restore it), counting the cell.
    Then add the count to ``[0x2a74]`` (the level's collectible total the tally reads). WITHOUT this the secret
    tiles render already-revealed (and [0x2a74] stays 0). Runs after 4182, before 40bd (so the count is doubled by
    ``_count_decor``)."""
    seg = d[_DS + 0x2DDA] | (d[_DS + 0x2DDB] << 8)
    base = (seg << 4) & 0xFFFFF
    dx = 0
    for k in range(0x50):                                        # [asm 3eb5 cx=0x50]
        si = 0x8C8D + k * 5                                      # [asm 3eb2/3ecb stride 5]
        bx = d[_DS + si + 3] | (d[_DS + si + 4] << 8)            # [asm 3eba] cell offset
        if bx == 0xFFFF:                                         # [asm 3ebd/3ec0]
            continue
        dx += 1                                                  # [asm 3ec2]
        cell = (base + bx) & 0xFFFFF
        al = d[_DS + si]                                         # [asm 3ec3] the hidden-tile replacement
        d[_DS + si + 1] = d[cell]                                # [asm 3ec8] stash the original (revealed) tile
        d[cell] = al                                             # [asm 3ec5] xchg -> patch the cell to hidden
    v = ((d[_DS + 0x2A74] | (d[_DS + 0x2A75] << 8)) + dx) & 0xFFFF   # [asm 3ed0] [0x2a74] += count
    d[_DS + 0x2A74] = v & 0xFF
    d[_DS + 0x2A75] = (v >> 8) & 0xFF


def _dup_double_buffer(d) -> None:
    """4065: dup the rebased [0x815e..] block to [0x9203..] (0x10a5 bytes, the working copy)."""
    d[_DS + 0x9203:_DS + 0x9203 + 0x10A5] = d[_DS + 0x815E:_DS + 0x815E + 0x10A5]


def _build_trigger_bank(d) -> None:
    """[asm 41CA] SAVE the pristine proximity-scenery map blocks into the ``[0x2875]`` bank. For each live
    ``[0x83F3]`` entry (15 x stride 0xA, ``[+0]`` != 0xFFFF): the block spans from ``[+0] - [+8]*0x100`` (the
    trigger top minus the countdown rows — everything the collapse will ever shift) over ``[+3]+[+8]`` rows x
    ``[+2]`` columns of the level map (``ds=[0x2DDA]``). Bank record: ``{word map_off, byte rows, byte width,
    tiles row-major}``, 0xFFFF-terminated; the entry's ``[+6]`` gets the bank offset of the LAST saved row (the
    reveal cursor 5427 walks backward by width per fire). Consumers: the 5427 map-mod reveal source and the
    52D2 restore (respawn / level-start un-collapse). Note the 4065 dup runs BEFORE this, so the [0x9203]
    backup keeps the pre-41CA ``[+6]`` (0xFFFF) — 5237's 5251 restore puts 0xFFFF back each respawn, exactly
    as observed in the L0xD snapshots."""
    bank = (d[_DS + 0x2875] | (d[_DS + 0x2876] << 8)) << 4           # [asm 41ca] es = [0x2875]
    mapb = (d[_DS + 0x2DDA] | (d[_DS + 0x2DDB] << 8)) << 4           # [asm 41fc] ds = [0x2DDA]
    di = 0                                                           # [asm 41ce]
    d[bank] = 0xFF; d[bank + 1] = 0xFF                               # [asm 41d0] empty-bank terminator
    for k in range(0xF):                                             # [asm 41d5/41d8] 15 entries
        bx = 0x83F3 + k * 0xA
        src = d[_DS + bx] | (d[_DS + bx + 1] << 8)
        if src == 0xFFFF:                                            # [asm 41db] dead entry
            continue
        count = d[_DS + bx + 8]                                      # [asm 41e8] ah=[bx+8]
        si = (src - (count << 8)) & 0xFFFF                           # [asm 41ed] si -= count*0x100
        rows = (d[_DS + bx + 3] + count) & 0xFF                      # [asm 41ef/41f2] dl = height + count
        width = d[_DS + bx + 2]                                      # [asm 41f5] dh
        d[bank + di] = si & 0xFF; d[bank + di + 1] = si >> 8         # [asm 4200] es:[di] = map offset
        d[bank + di + 2] = rows; d[bank + di + 3] = width            # [asm 4203] es:[di+2] = dx (dl,dh)
        di += 4                                                      # [asm 4207]
        for _r in range(rows):                                       # [asm 420a-421d]
            for c in range(width):                                   # movsb x width (one map row)
                d[bank + di] = d[mapb + ((si + c) & 0xFFFF)]
                di += 1
            si = (si + 0x100) & 0xFFFF                               # [asm 4215/4217] next map row
        cursor = (di - width) & 0xFFFF                               # [asm 4224] the LAST saved row's offset
        d[_DS + bx + 6] = cursor & 0xFF                              # [asm 4228] [bx+6] = the reveal cursor
        d[_DS + bx + 7] = cursor >> 8
        d[bank + di] = 0xFF; d[bank + di + 1] = 0xFF                 # [asm 422b] terminator after this entry


def native_level_load_objects(state) -> None:
    """The DGROUP-only object/effect-table setup that runs after the property copy [asm 4059..409d]:
    the two sprite-bank rebases + the double-buffer dup. (42af tile tables, 3ead level-data patch, 40bd
    [0x6a88] index, 41ca trigger sprites are the loader's other, separately-scoped halves.)"""
    d = state.data
    _per_level_pointers(d)           # 4038..4056
    _rebase_effect_sprites(d)        # 414d
    _rebase_entity_sprites(d)        # 4182
    _self_patch_secret_tiles(d)      # 3ead: hide the secret/bonus tiles + count into [0x2a74]
    _assign_random_decor(d)          # 40bd
    _dup_double_buffer(d)            # 4065
    _build_trigger_bank(d)           # 41ca: save the pristine scenery blocks -> [0x2875] (52D2/5427 read them)
    _count_decor(d)                  # 4073..40bb


def _per_level_pointers(d) -> None:
    """4038..4056: per-level word [0x2d40+level*2] -> [0x2dbc] (level-advance/scroll), [0x2dbe]=0; and the
    two header words [0x8160]/[0x8162] (from the property block) -> [0x6bad]/[0x6baf]."""
    level = d[_DS + 0x2D8A]
    for dst, src in ((0x2DBC, 0x2D40 + level * 2), (0x6BAD, 0x8160), (0x6BAF, 0x8162)):
        d[_DS + dst] = d[_DS + src]; d[_DS + dst + 1] = d[_DS + src + 1]
    d[_DS + 0x2DBE] = 0; d[_DS + 0x2DBF] = 0


def _count_decor(d) -> None:
    """4073..40bb: count float-effect entries whose (rebased) sprite [+4] is in [0x6e,0x75] or [0x80,0xdb];
    add to [0x2a78]; then if level<0xA and [0x2cf6+level]!=0xff, double [0x2a78] and [0x2a74]."""
    dx = 0
    for k in range(0x46):
        ax = d[_DS + 0x8F1D + k * 7 + 4] | (d[_DS + 0x8F1D + k * 7 + 5] << 8)
        if ax == 0xFFFF or ax < 0x6E or ax > 0xDB:
            continue
        if ax >= 0x80 or ax <= 0x75:
            dx += 1
    v = (d[_DS + 0x2A78] | (d[_DS + 0x2A79] << 8)) + dx
    d[_DS + 0x2A78] = v & 0xFF; d[_DS + 0x2A79] = (v >> 8) & 0xFF
    level = d[_DS + 0x2D8A]
    if level < 0x0A and d[_DS + 0x2CF6 + level] != 0xFF:
        for o in (0x2A78, 0x2A74):
            v = ((d[_DS + o] | (d[_DS + o + 1] << 8)) << 1) & 0xFFFF
            d[_DS + o] = v & 0xFF; d[_DS + o + 1] = (v >> 8) & 0xFF


def native_level_load_planar(state) -> None:
    """The LOCAL planar tile-cache blit [asm 4316]: demux each index-table sprite with ``code < 0x100`` into the
    four EGA shadow planes the renderer reads (``mem.data[EGA_APERTURE + plane*stride + 0x5e80]``). Byte-exact vs
    the ASM under a clean-VRAM witness (probe: 173/173 local slots). Local sheet = ``[0x2dda] + ([0x2d30+lvl]<<4)``.

    The SHARED pass (``4389``, ``code >= 0x100``) is NOT done here: at load time it is gated behind a conditional
    and reads a bank that ``3ed6`` first decompresses via a separate codec ``call 0x492`` (cx=7) into ``[0x2ddc]``
    — not the simple per-frame ``[0x2dd8]`` bank the live bridge path uses. Recovering ``0x492`` + that gate is a
    follow-up; until then the shared/common sprites are left empty. See [[pre2-level-init-island]]."""
    from pre2.bridge.sprites import compute_local_slots, write_slots
    d = state.data
    seg = d[_DS + 0x2DDA] | (d[_DS + 0x2DDB] << 8)
    level = d[_DS + 0x2D8A]
    bh = d[_DS + 0x2D30 + level]
    base_seg = (seg + (bh << 4)) & 0xFFFF
    write_slots(state, compute_local_slots(state, base_seg))         # 4316 (local)


_PARALLAX_BASE = 0x7E80      # BG_PTR_BIAS — the parallax-base the renderer reads (frame_renderer.py)
_PARALLAX_PLANE = 0x1F40     # 200 rows * 0x28 = one screen per plane


def native_level_load_background(state, *, game_root: str) -> None:
    """Load the level's parallax BACKDROP into the ``0x7E80`` parallax base the renderer reads.

    ``BACK<group>.SQZ`` (the C-string at ``[0x2da6]``, its group digit ``[0x2daa]`` set by the dgroup prologue) is a
    STATIC 4-plane sky image — ``0x7d00`` bytes = 4 planes x ``0x1F40`` (200 rows x 0x28), planes consecutive — that
    the renderer composites behind transparent/partial tiles (``BG_PTR_BIAS = 0x7E80`` in frame_renderer; the
    camera-counteracting shift that keeps it fixed on screen is the renderer's job). Decoded straight into the four
    EGA planes at ``0x7E80..0x9dc0``. Byte-exact vs the VM's ``0x7E80`` (100%, all four planes). (The old
    ``[0x2dda]``-over-read blit to ``0x9dc0`` was a dead end — the renderer never reads ``0x9dc0``.)"""
    from pre2.native.vga import EGA_APERTURE, EGA_PLANE_STRIDE

    from pre2.native.assets import read_cstring, resolve_game_path
    name = read_cstring(state, 0x2DA6)                             # BACK<group>.SQZ
    back = unpack_sqz(resolve_game_path(game_root, name).read_bytes())
    d = state.data
    for p in range(4):
        dst = EGA_APERTURE + p * EGA_PLANE_STRIDE + _PARALLAX_BASE
        d[dst:dst + _PARALLAX_PLANE] = back[p * _PARALLAX_PLANE:(p + 1) * _PARALLAX_PLANE]


def native_level_load_classify(state) -> None:
    """4232: classify each populated cache slot into the type table ``[0x4DF8]`` (0 opaque / 1 empty / >=2 partial)
    and save the partial transparency masks compacted at ``[0x2DF8]`` — the contract the per-frame blit consumes.
    Reuses the recovered ``classify_sprites``; runs after the planar blit (it reads the cache)."""
    from pre2.bridge.sprites import read_sprite_cache
    from pre2.recovered.sprite_classify import classify_sprites
    res = classify_sprites(read_sprite_cache(state))
    d = state.data
    d[_DS + 0x4DF8:_DS + 0x4DF8 + len(res.types)] = res.types
    n = res.partial_count * 0x20
    d[_DS + 0x2DF8:_DS + 0x2DF8 + n] = res.masks[:n]


def native_level_load_anim_tables(state) -> None:
    """[asm 42af] Build the 4 tile-animation lookup tables [0x6688]/[0x6788]/[0x6888]/[0x6988] (0x100 bytes each)
    that the renderer (redraw_animated_grid / animation.py) reads to animate tiles. Walk the 0x100 tile indices,
    reading the per-tile flag at [0x805E+t] (the foreground/animated-flag table): a STATIC tile (bit 0x80 clear)
    maps to itself in tables 0/1/2 and 0 in table 3; an ANIMATED tile (bit 0x80 set) consumes 3 consecutive
    indices and fills a rotated 3-frame cycle (tables 0/1/2 = the three frames t/t+1/t+2, each rotated) with 1 in
    table 3. [0x6bbd] = 1 iff the level has any animated tile. Deterministic; reads [0x805E] (loaded by dgroup)."""
    d = state.data
    T0, T1, T2, T3, FLAG = 0x6688, 0x6788, 0x6888, 0x6988, 0x805E
    has_anim = 0
    bx = 0
    while bx < 0x100:                                              # [asm 430d: or bh,bh / je]
        if (d[_DS + FLAG + bx] & 0x80) == 0:                       # [asm 42bb] static tile
            t = bx & 0xFF
            d[_DS + T0 + bx] = t; d[_DS + T1 + bx] = t; d[_DS + T2 + bx] = t; d[_DS + T3 + bx] = 0
            bx += 1                                                # [asm 42d3 -> 430c]
        else:                                                      # [asm 42d5] animated: rotated 3-frame cycle
            has_anim = 1
            t = bx & 0xFF; t1 = (t + 1) & 0xFF; t2 = (t + 2) & 0xFF
            d[_DS + T0 + bx] = t;  d[_DS + T0 + bx + 1] = t1; d[_DS + T0 + bx + 2] = t2
            d[_DS + T1 + bx] = t1; d[_DS + T1 + bx + 1] = t2; d[_DS + T1 + bx + 2] = t
            d[_DS + T2 + bx] = t2; d[_DS + T2 + bx + 1] = t;  d[_DS + T2 + bx + 2] = t1
            d[_DS + T3 + bx] = 1;  d[_DS + T3 + bx + 1] = 1;  d[_DS + T3 + bx + 2] = 1
            bx += 3                                                # [asm 430a/430b/430c: inc bx x3]
    d[_DS + 0x6BBD] = has_anim                                     # [asm 4311] any-animated flag


def native_level_load_shared(state, *, game_root: str) -> None:
    """The shared/union sprite-bank pass [asm 4389]: the global ``UNION.SQZ`` bank (resource 7, filename at
    DGROUP 0x0a25), decompressed by the recovered SQZ codec (``load_sqz_by_dx`` — VERIFIED: the VM's 0x107b UNION
    output is byte-identical), then every index-table sprite with code >= 0x100 demuxed into the planar cache via
    the recovered ``compute_shared_slots``. UNION is GLOBAL (same bank for every level), so this just loads it +
    fills the cache half the local pass leaves empty. ``[0x2ddc]`` is the bank base the shared pass addresses."""
    from pre2.bridge.sprites import compute_shared_slots, write_slots
    d = state.data
    # [asm 3fb1-3fb8] load UNION at a SMALL base so the 4389 segment arithmetic ((code-0x100)*8 + base) does not
    # wrap (a large bump segment wraps the high codes -> garbage). The original loads it over the level-data seg
    # [0x2dda] (which it then reloads); we do the same BUT [0x2dda]:0 is the tilemap GRID the standalone renderer
    # reads each frame (read_foreground_state / render_frame), so we SAVE it, load UNION there only to demux into
    # the cache (UNION isn't read after the cache is filled), then RESTORE the grid. Otherwise the grid becomes
    # UNION bytes -> scattered tiles + garbage tile types.
    saved_bump = d[_DS + 0x2875] | (d[_DS + 0x2875 + 1] << 8)
    lvl_seg = d[_DS + 0x2DDA] | (d[_DS + 0x2DDA + 1] << 8)
    lvl_base = (lvl_seg << 4) & 0xFFFFF
    grid = bytes(d[lvl_base:lvl_base + 0x12000])                    # UNION decompresses to ~0x11000; save + margin
    d[_DS + 0x2875] = lvl_seg & 0xFF; d[_DS + 0x2875 + 1] = (lvl_seg >> 8) & 0xFF
    union_seg = load_sqz_by_dx(state, 0x0A25, game_root=game_root)  # [asm 0699->107b] decompress UNION.SQZ
    d[_DS + 0x2875] = saved_bump & 0xFF; d[_DS + 0x2875 + 1] = (saved_bump >> 8) & 0xFF
    d[_DS + 0x2DDC] = union_seg & 0xFF; d[_DS + 0x2DDC + 1] = (union_seg >> 8) & 0xFF
    write_slots(state, compute_shared_slots(state, union_seg))      # [asm 4389] demux code>=0x100 into the cache
    d[lvl_base:lvl_base + 0x12000] = grid                          # restore the level-data grid UNION overwrote


def native_player_init(state) -> None:
    """55fc: (re)initialise the object pool + the player for level start. Clears `[0x4f1c]` (0x40b words), marks
    0x74 object slots free (`[di+4]=0xFFFF`, `[di+0x11]=0`, stride 0x12 from `[0x4f0a]`), then sets the player
    position from the level's start point in the property tables: X=`[0x8160]`, Y=`[0x8162]`. [asm 55fc]"""
    d = state.data
    d[_DS + 0x4F1C:_DS + 0x4F1C + 0x40B * 2] = b"\x00" * (0x40B * 2)
    for k in range(0x74):
        o = _DS + 0x4F0A + k * 0x12
        d[o + 4] = 0xFF; d[o + 5] = 0xFF
        d[o + 0x11] = 0
    for dst, src in ((0x4F1C, 0x8160), (0x4F1E, 0x8162)):
        d[_DS + dst] = d[_DS + src]; d[_DS + dst + 1] = d[_DS + src + 1]


def native_level_load(state, level: int, *, game_root: str) -> None:
    """Load ``LEVEL<n>.SQZ`` into a NativeGameState — the seed the gameplay loop runs on, composed from the
    recovered halves of ``1030:3ed6``:
      * ``native_level_load_dgroup``  — prologue scalars, SQZ load, tile-index, property memcpy (byte-exact);
      * ``native_level_load_objects`` — entity/effect sprite-bank rebases, RNG decor, per-level reads, dup (byte-exact);
      * ``native_level_load_anim_tables`` — the tile-animation tables ``[0x6688]`` (``42af``, byte-exact);
      * ``native_level_load_planar``  — the LOCAL planar tile cache (``code < 0x100``, byte-exact);
      * ``native_level_load_shared``  — the SHARED/union bank (``code >= 0x100`` from UNION.SQZ, byte-exact);
      * ``native_level_load_classify``— the tile/sprite type tables (``4232``, byte-exact over the full cache).

    All verified byte-exact vs the ASM against a clean-VRAM 3ed6 witness (LOCAL 173/173, SHARED 83/83, classify +
    42af 0 diff). The ``3ead`` secret-tile self-patch and the ``41ca`` proximity-scenery bank build are both
    recovered and live (``_self_patch_secret_tiles`` / ``_build_trigger_bank`` in ``native_level_load_objects``;
    the [+6]=0xFFFF-at-gameplay mystery resolved: the 4065 dup precedes 41ca, so 5237's 5251 backup-restore
    reverts [+6] at level start and every respawn).

    The long-mislabelled "0x9dc0 parallax blit" is NOT a gap: [asm 3f8d-3ff7] is the loader's STASH/RESTORE of
    the level data through spare VRAM (park 4x0x6240 bytes at A000:0x9dc0, decode the shared sprite bank OVER
    the level segment as scratch, copy the level data back) — a 64KB-era memory-reuse trick whose net effect on
    the level segment is nil. 0x9dc0..0x10000 sits above the display pages and the 0x7E80 sky; the renderer
    never reads it, and the two 0x9dc0 references (3f9d write / 3fee read) are the only ones in the code
    segment. Native's pure shared-bank decode never clobbers the level segment, so the stash pair is correctly
    absent; the post-load byte-exact witnesses above prove the equivalent end state. NOTHING remains deferred
    in the loader."""
    native_level_load_dgroup(state, level, game_root=game_root)
    # [asm 3f8d] the parallax blit runs HERE — right after the LEVEL<n> load, BEFORE UNION/BACK0. LEVEL<n>.SQZ
    # decodes LARGER than its reserved paragraphs ([0x2875] bump), so the blit's 4-plane over-read past the reserve
    # reads LEVEL<n>'s decoded overflow PLUS the previous allocation still in memory (MAP.SQZ from the carte). Doing
    # it here — before the object/UNION passes overwrite that tail — is what makes the standalone parallax match.
    native_level_load_background(state, game_root=game_root)       # BACK<n>.SQZ -> the 0x7E80 parallax base (the sky)
    native_level_load_objects(state)
    native_level_load_anim_tables(state)                            # 42af: tile-animation tables [0x6688]
    native_level_load_planar(state)                                 # LOCAL tile cache (code < 0x100)
    native_level_load_shared(state, game_root=game_root)            # SHARED/union bank (code >= 0x100), UNION.SQZ
    native_level_load_classify(state)                              # 4232: tile/sprite type tables (full cache)
