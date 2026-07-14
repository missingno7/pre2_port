"""The GAME OVER scene — recovered native driver for [asm 9B23..9DFB] (the 5063 game-over tail).

After the death-bounce, the VM shows the GAMEOVER.SQZ diorama with the falling GAME-OVER letters, the
crying caveman + friends, and three circling birds, scrolling down to the tableau, until the fire key or
a ~9 s timeout, then DAC-fades out. The RENDER side is already recovered (bridge/gameover_scene
``build_gameover_scene`` = diorama background + object/HUD overlays, the hybrid faithful renderer's path);
this module recovers the STATE side:

  * setup [9B35..9C5F]: camera reset, the 16-colour palette at DGROUP 0xAFE8, free every object slot, spawn
    the 8 letters (ids from the [0xB018] table + 0xB0, staggered X, bounce seed from the 39DF RNG), the 4
    tableau characters, and the 3 bird records (phase 0/0x55/0xAA + anim scripts 0xB020/32/54);
  * the per-frame tick [9CC0..9DD9]: scroll [0x6BC4] to 0xB9, the crying-caveman anim cycle (0x68..0x6D
    every 4th frame), the letters' bounce oscillator, the birds' circular orbit (sin/cos tables DS:0x6F90/
    0x7090, phase +2, H-flip from the Y sign) + Y-sort + the near-side handoff into the render slots;
  * the loop protocol [9C62..9C86]: tick + render + input, exit on fire or [0x27F0] >= 0x276, 9286 fade.

[0x2879]!=0 (demo playback) skips the whole scene, exactly as the ASM's 9B2C gate."""
from __future__ import annotations

from pre2.views.dgroup_view import PlayerGlobals, RngView
from pre2.views.memory_adapter import apply_ds, readers
from pre2.views.tables import ByteTable
from pre2.native.state import DATA_SEG
from pre2.native.vga import _dac8
from pre2.recovered.input_decode import decode_input
from pre2.native.dgroup_offsets import (
    BIRD_PAIR_SLOTS, CRY_CAVEMAN_SPRITE, GAMEOVER_PALETTE, PLAYER_SLOT, RENDER_SLOTS_BASE)

_DS = DATA_SEG << 4

_LETTER_TABLE = 0xB018      # [asm 9BA3] 8 bytes: sprite id - 0xB0 per GAME/OVER letter
_BIRD_BASE = 0x5138         # [asm 9C35] the 3 bird records
_BIRD_SIN = 0x6F90          # [asm 9D15] DS: signed sine table (X orbit)
_BIRD_COS = 0x7090          # [asm 9D3F] DS: signed cosine table (Y orbit)
_TIMEOUT = 0x276            # [asm 9C74] idle frames (70Hz) before the scene auto-exits


def _s8(v):
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _rd(state, off):
    return state.rw(off & 0xFFFF)


def _ww(state, off, val):
    state.ww(off & 0xFFFF, val)


def native_gameover_setup(state) -> None:
    """[asm 9B35..9C5F] Build the scene state (camera, slots, letters, tableau, birds). The GAMEOVER.SQZ
    image + palette are the render/runner side (bridge gameover_background + the 0xAFE8 DAC load)."""
    g = PlayerGlobals(state)
    g.cam_col_word = 0; g.cam_row_word = 0; g.row_factor = 0       # [9B35-9B3B] camera reset
    g.fine_scroll = 0                                              # [9B3E] scroll = top
    for i in range(0x74):                                          # [9B8C-9B9B] free every object slot
        _ww(state, RENDER_SLOTS_BASE + i * 0x12 + 4, 0xFFFF)
    # --- the 8 GAME/OVER letters [9B9D-9BF1]: ids [0xB018+i]+0xB0, X staggered 0x18 (+0x30 gap after 4),
    #     Y=0xE0, bounce seed [di+0xE] = rand&7 (forced nonzero for the first 4) ---
    letters = ByteTable(state.rb, _LETTER_TABLE)
    di, x = PLAYER_SLOT, 0x2C
    for i in range(8):
        if i == 4:
            x = (x + 0x30) & 0xFFFF                                # [9BD1] the GAME|OVER word gap
        _ww(state, di + 4, (letters[i] + 0xB0) & 0xFFFF)
        _ww(state, di, x)
        _ww(state, di + 2, 0xE0)
        r = RngView(state).roll() & 7                                  # [9BBA/9BE2] 39DF & 7
        if i < 4 and r == 0:                                       # [9BC0-9BC2] first word: nonzero seed
            r = 1
        _ww(state, di + 0xE, r)
        x = (x + 0x18) & 0xFFFF
        di += 0x12
    # --- the 4 tableau characters [9BF3-9C34] (below the fold; the scroll reveals them) ---
    di += 0xB4                                                     # [9BF3] -> 0x5060
    for x, y, sid in ((0x96, 0x135, 0x64), (0x97, 0x108, 0x1C4), (0x96, 0x121, 0x68), (0x96, 0x119, 0x62)):
        _ww(state, di, x); _ww(state, di + 2, y); _ww(state, di + 4, sid)
        di += 0x12
    # --- the 3 circling birds [9C35-9C5B]: phase + anim-script ptr (X/Y/id computed per tick) ---
    for k, (phase, script) in enumerate(((0, 0xB020), (0x55, 0xB032), (0xAA, 0xB054))):
        base = _BIRD_BASE + k * 0x12
        state.wb(base + 8, phase)
        _ww(state, base + 0xC, script)
    g.idle_clock = 0                                               # [9C5C] reset the idle/timeout counter


def native_gameover_tick(state) -> None:
    """[asm 9CC0..9DD9] One scene frame: scroll, caveman-cry anim, letter bounce, bird orbits + sort +
    near-side handoff. Transcribed instruction-for-instruction."""
    d = state.data
    g = PlayerGlobals(state)
    if g.fine_scroll < 0xB9:                                       # [9CC6-9CCD] scroll down to the tableau
        g.fine_scroll += 1
    if (g.frame_stamp & 3) == 0:                                   # [9CD1-9CD6] every 4th frame
        ax = _rd(state, CRY_CAVEMAN_SPRITE)                                        # [9CD8] the crying caveman's sprite id
        ax = ((ax & 0x1FFF) + 1) & 0xFFFF                          # [9CDB-9CDE] and ah,0x1f ; inc
        if ax >= 0x6E:                                             # [9CDF-9CE4] cycle 0x68..0x6D
            ax = 0x68
        _ww(state, CRY_CAVEMAN_SPRITE, ax)
    si = PLAYER_SLOT
    for _ in range(8):                                             # [9CEA-9D04] the letters' bounce oscillator
        ax = (_rd(state, si + 0xE) + 1) & 0xFFFF                       # [9CF0-9CF3] vel += 1
        if _s8(ax) >= 8:                                           # [9CF4-9CF6] cmp al,8 ; jl
            ax = (-ax + 1) & 0xFFFF                                # [9CF8-9CFA] neg ; inc
        _ww(state, si + 0xE, ax)
        _ww(state, si + 2, (_rd(state, si + 2) + _s16(ax)) & 0xFFFF)       # [9CFE] Y += vel
        si += 0x12
    si = _BIRD_BASE
    for _ in range(3):                                             # [9D06-9D95] the bird orbits
        _ww(state, si - 0x164, 0xFFFF)                                 # [9D0C] free the paired render slot
        phase = state.rb(si + 8)
        sin = _s8(state.rb(_BIRD_SIN + phase))                     # [9D12-9D19] xlatb + cbw
        _ww(state, si, (0x96 + ((sin * 0x41) >> 6)) & 0xFFFF)          # [9D1A-9D3A] X = 0x96 + sin*0x41/64
        cos = _s8(state.rb(_BIRD_COS + phase))                     # [9D3C-9D43]
        v = (cos * 0x0A) >> 6                                      # [9D44-9D5F] the Y orbit component
        _ww(state, si + 6, v & 0xFFFF)                                 # [9D61]
        _ww(state, si + 2, (v + 0x135) & 0xFFFF)                       # [9D64-9D67] Y = v + 0x135
        state.wb(si + 8, (phase + 2) & 0xFF)                       # [9D6A] phase advance
        bx = _rd(state, si + 0xC)                                      # [9D6E] the anim-script advance
        ax = _rd(state, bx)
        while ax & 0x8000:                                         # [9D73-9D79] loop marker: rewind
            bx = (bx + _s16(ax)) & 0xFFFF
            ax = _rd(state, bx)
        ax = (ax + 0x138) & 0xFFFF                                 # [9D7B]
        ax |= _rd(state, si + 6) & 0x8000                              # [9D7E-9D85] H-flip on the far side
        _ww(state, si + 4, ax)                                         # [9D87]
        _ww(state, si + 0xC, (bx + 2) & 0xFFFF)                        # [9D8A-9D8C]
        si += 0x12
    # [9D98-9DB1] bubble-sort the 3 bird records DESCENDING by Y (swap + restart on any inversion)
    swapped = True
    while swapped:
        swapped = False
        for k in range(2):
            a = _BIRD_BASE + k * 0x12
            b = a + 0x12
            if _s16(_rd(state, b + 2)) >= _s16(_rd(state, a + 2)):         # [9DA4-9DA7] jl skips the swap
                pa, pb = _DS + a, _DS + b
                d[pa:pa + 0x12], d[pb:pb + 0x12] = d[pb:pb + 0x12], d[pa:pa + 0x12]   # [9DDA] record swap
                swapped = True                                     # [9DAC] jmp 9D98 (restart)
                break
    # [9DB3-9DD1] the FIRST TWO bird slots: a bird on the near side (Y >= 0x135) moves to the render slots
    # at 0x4FD0/0x4FE2 (drawn earlier = behind the tableau) and its 0x5138 record is freed for this frame
    for k in range(2):
        si = _BIRD_BASE + k * 0x12
        di = BIRD_PAIR_SLOTS + k * 0x12
        if _s16(_rd(state, si + 2)) >= 0x135:                          # [9DBC-9DC1]
            ps, pd = _DS + si, _DS + di
            d[pd:pd + 0x12] = d[ps:ps + 0x12]                      # [9DF0] copy the record
            _ww(state, si + 4, 0xFFFF)                                 # [9DC6] free the original


def native_gameover_scene(state, dos, game_root: str):
    """[asm 9B23..9C86] Drive the whole GAME OVER scene, yielding one ``(planes, page)`` per SCENE ITERATION
    (~23Hz — each 44FB present busy-waits 3 retraces, so the loop runs 0x276/3 iterations before the [0x27F0]
    timeout). Runs setup, then tick+render until the fire key or the timeout, then the 9286 DAC fade-out
    (yielded over the frozen last frame). The caller presents each frame at _FRONT_END_FPS/3 and drives the key
    table (DC1 sources) between yields.

    [asm 9B2C] demo playback ([0x2879]!=0) skips the scene entirely."""
    from pre2.views.gameover_scene import build_gameover_scene
    from pre2.native.render import native_load_dac_palette
    from pre2.recovered.front_end_fade import fade_out_frames

    d = state.data
    g = PlayerGlobals(state)
    if g.input_source != 0:                                        # [9B2C-9B32] demo mode: no scene
        return
    native_gameover_setup(state)
    native_load_dac_palette(state, dos, 0xAFE8)                    # [9B7F-9B8A] int10 AX=1012 from DS:0xAFE8
    g.frame_stamp = 0                                             # deterministic anim phase for the cry cycle
    rb, rw = readers(state)
    last = None
    # [9C74] The idle timeout is [0x27F0] >= 0x276, and [0x27F0] is bumped by the 70Hz TIMER ISR — but each scene
    # iteration does one 44FB present that busy-waits ~3 retraces (9C6B 44FB + 9C71 990D), so the timer fires ~3x
    # per iteration ([0x27F0] += 3). The scene runs 0x276/3 = 0xD2 (210) ITERATIONS, each advancing the animation
    # once and DISPLAYING for 3 retraces (~23Hz, not 70Hz). Looping the full 0x276 ran the cry/letters/birds anim
    # 3x too fast (user: "game-over scene faster than it should be"). The caller presents at _FRONT_END_FPS/3 so
    # the ~9s wall-clock duration is preserved.
    for frame in range(_TIMEOUT // 3):                            # [9C74] 0x276/3 presents (timer +3 per iteration)
        native_gameover_tick(state)                                # [9C62] 9CC0
        g.frame_stamp = (g.frame_stamp + 1) & 0xFFFF              # the frame counter the cry cycle reads
        g.idle_clock = (g.idle_clock + 3) & 0xFFFF                # the idle counter the ASM times out on
        planes, _status = build_gameover_scene(state, dos, game_root=game_root, page=0)  # [9C65/9C68] 9C87+26FA
        last = planes
        yield planes, 0                                            # [9C6B] 44FB present
        apply_ds(state, decode_input(rb, rw))                      # [9C6E] DC1
        if g.in_fire:                                               # [9C7C-9C81] fire exits early
            break
    # [9C83] 9286: the 16-colour DAC fade-out over the frozen frame
    pal6 = bytes(b & 0x3F for b in d[_DS + GAMEOVER_PALETTE:_DS + GAMEOVER_PALETTE + 0x10 * 3])
    for snap in fade_out_frames(pal6, 0x10):
        for i in range(0x10):
            dos.vga_palette[i] = (_dac8(snap[i * 3]), _dac8(snap[i * 3 + 1]), _dac8(snap[i * 3 + 2]))
        if last is not None:
            yield last, 0
