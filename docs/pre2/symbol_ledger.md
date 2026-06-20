# PRE2 symbol / address ledger

Important original `PRE2.EXE` addresses, continuation points, and state locations
used by recovered code. The original binary/ASM is the oracle; entries here are
*candidates* until a verifier proves them.

Addresses are in the unpacked image (post-LZEXE); segment `1030` is the main game
code segment in the current VM layout, `1A13` is a fixed data segment.

**Columns:** location · name · confidence · role · verifier/test coverage · known unknowns.
**Confidence:** GUESS · OBSERVED · ASM_MATCHED · VERIFIED · CANONICAL.
**Role:** probe · checkpoint/verifier · replacement · data · canonical.

## Audio / SoundBlaster (IMPLEMENTED — generic hw in dos_re; gameplay audio plays)

Emulated as generic PC hardware: `dos_re/sblaster.py` (SB DSP + 8237 DMA channel,
8-bit unsigned PCM), `dos_re/pic.py` (8259), wired via `runtime.enable_sound_blaster`
(live viewer only). The DSP→bump uses `sqz_bump_advance` (LZSS pre-shift fixed).
Verified: detection passes, IRQ auto-detect succeeds, blocks stream; `--verify-hooks`
shows the bump matches (no divergence). Below is the probe map that got us there.

PRE2 **auto-detects** the SoundBlaster (no `BLASTER` env needed if the hw responds).
Assets (confirmed): SFX = 8-bit signed PCM @ 8000 Hz, 11 effects (`SAMPLE.SQZ`,
60768 B, our "other" codec); music = `.TRK` = LZSS/EAT-compressed ProTracker MOD.
The game software-mixes MOD + SFX to PCM and streams it via SB DMA.

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:1D42`/`1D4F` | **SB base-scan + DSP reset detect** — for base `0x210..0x260` (step 0x10): `OUT base+6` =1,delay,=0; poll `base+0xE` bit7 (≤2000); read `base+0xA` == `0xAA`. On hit stores ports `cs:[0x266]=base`, `[0x268]=base+0xA` (read-data), `[0x26A]=base+0xC` (write-cmd), `[0x26C]=base+0xE` (read-status/IRQ-ack) | OBSERVED | (hw probe) | captured live (cold boot → reset `0x216`, poll `0x21E`) | — |
| `1030:1F6D` | **IRQ auto-detect setup** — swaps 8 IVT vectors (INT 08–0Fh) for counting ISRs (`1FAA`/`1FC0`/`1FD6`/…): each reads `cs:[0x26C]` (= `base+0xE`, SB IRQ ack), bumps a per-IRQ counter `[0xE67…]`, `OUT 0x20,0x20` (EOI), chains old vector. Triggers an SB IRQ and sees which counter moved → the SB IRQ | OBSERVED | (hw probe) | — | how the trigger transfer / DMA channel is auto-detected (next) |
| ports `base+6/0xA/0xC/0xE` | SB DSP: reset / read-data / write-cmd+status / read-buffer-status+IRQ-ack | OBSERVED | (hw) | — | DSP command set the driver uses (0x14/0x1C/0x40/0x41/0xD1/0xF2…) — capture once detection passes |
| 8237 DMA ch + page, PIC `0x20/0x21` | DMA channel for PCM + SB IRQ via PIC | GUESS | (hw) | — | which DMA channel; needs the playback capture |

## SQZ decompressor (first recovered island → merges into the asset loader)

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:1068` | `sqz_decompress` — decompressor public entry (opens file at `1A13:DX`, dispatches, decodes, returns `ax`=out seg) | VERIFIED | replacement | `pre2/replacements.py`; `--verify-hooks`; `tests/test_sqz_codec.py` | — |
| `1030:00EC` / `00EF` | caller `call 1068` / return (`mov [3b],ax`) | ASM_MATCHED | (caller) | hook near-rets here via `cpu.pop()` | other callers (KEYB returns elsewhere) |
| `1030:10B4` | LZSS dispatch: `cmp ax,0x4cb4` (word[0]) | ASM_MATCHED | (branch) | dispatch matched in `unpack_sqz` | — |
| `1030:10BC` | LZW dispatch: `cmp ah,0x10` (data[1]) | ASM_MATCHED | (branch) | dispatch matched in `unpack_sqz` | — |
| `1030:1401`–`148E` | LZSS header setup (17-byte header read, size/reserve calc, read-size `[13FD]`) | ASM_MATCHED | (setup) | `sqz_reserved_size()` | exact meaning of wrapper byte 9 (00/01) |
| `1030:148F`–`16E3` | LZSS decode (bp/dl LSB-first bit reader; bit1=literal/bit0=match; back-ref copy `1501`) | VERIFIED | replacement | `unpack_sqz_lzss`; sprites/allfonts byte-exact | — |
| `1030:15EF` | LZSS exit `ret` | VERIFIED | verifier boundary | `_DECOMP_EXITS` (verify diff site) | — |
| `1030:1240`–`13F5` | LZW decode (CLEAR=0x100/END=0x101, 9–12-bit codes; `GET_BITS` MSB-first @`133B`) | VERIFIED | replacement | `unpack_sqz_lzw`; keyb byte-exact | — |
| `1030:1328` | LZW exit `ret` | VERIFIED | verifier boundary | `_DECOMP_EXITS` | — |
| `1030:10E6` / `11BD` | "other" Huffman+RLE decode / Huffman tree-walk reader (`rol ax,1`; `[bx+6]` nodes; leaf=bit15) | VERIFIED | replacement | `unpack_sqz_other`; sample byte-exact | — |
| `1030:11F0` | "other" exit `ret` | VERIFIED | verifier boundary | `_DECOMP_EXITS` | — |
| `1A13:2871` | output **bump allocator** (next free decompression segment) — advances by `(reserved>>4)+1` paragraphs | VERIFIED | data | `sqz_reserved_size()`; verify bump diff | **UNION.SQZ verify divergence**: loaded via `1030:047A` (not the plain bump path), so its post-decode `[0x2871]` ≠ `(reserved>>4)+1` heuristic — *bytes match*, only the bump expectation differs; hybrid gameplay works. Separate SQZ-island follow-up, exposed by verify-mode replay through level load. |
| `1030:11F3` | decompressor output-segment variable (`[11F3]`) | ASM_MATCHED | data | written by `_commit_native` | — |
| `1030:11F1` | decompressor file-handle variable (`[11F1]`) | OBSERVED | data | — | — |

## Sprite/tile render island (in progress)

The sprite-sheet **decode** sub-island (`42F7`/`436A`) is DONE/VERIFIED (above).
The current sub-island is the **draw primitive layer**.

**Island scope (boundary — keep it here, do not sprawl):**
- **IN:** the classifier `4213` (builds the blit's `[0x4DF4]` type table + `[0x2DF4]`
  compacted-sprite buffer from the decoded cache), and the blit primitive
  `3B69` + its paths (`3B7C` plain / `3BD7` masked / solid) + `3D65` bg-restore.
  Merge target: a `renderer` module — `classify_sprites(cache)` and
  `blit_sprite(idx, screen_off, …)`.
- **UPPER boundary (OUT — the NEXT island):** the tilemap / sprite-list **draw
  loops** (`34A0`, `3552`, callers of `3B58` at `65A0`/`8BFF`) that iterate game
  state (tilemap layout, object list, scroll position), build per-entry flags via
  `xlatb`, compute screen offsets, and call the blit. Also the background
  scroll/compose (`3A60`/`3A08` frame orchestrator). These own the game data model.
- **LOWER boundary:** EGA/VRAM hardware (the VM provides it).
- **Verification unit:** one blit call — inputs `(idx, screen_off, es, cache,
  [0x2DF4], bg buffer, GC/map-mask state)` → framebuffer delta (note the masked
  path's `xchg` also writes `[0x2DF4]`, a read/write contract).

### Next island — frame renderer / scroll engine (boundary MAPPED 2026-06-20)

**Merge target:** these routines are one landmass = the **frame renderer + scroll engine**. They
recover into a `pre2/recovered/frame_renderer.py` driven by a `Camera`/`ScrollState`/`TileMap` model in
`pre2/bridge/` — NOT a pile of per-routine hooks. Coastline note: the per-frame entry (`3B40`) and the
directional scroll routines (`3344/338E/33F5/…`) have **no direct callers in the code segment — they are
dispatched indirectly** (movement/direction dispatch; cf. `3300: call dx`). So the eventual checkpoint is a
**semantic frame/tick contract** (camera + dirty + framebuffer), not a static CALL hook. Capture witness +
build the Camera/TileMap bridge first (task #2); wire one thin frame-boundary adapter, not one-per-routine.

**Game data model (ds=1A13) — the emerging `Camera`/`ScrollState`/`TileMap`:**
`[0x2DE0]`=camera X (tile col), `[0x2DE2]`=camera Y (tile row); `[0x2DDC]/[0x2DDE]`=previous camera X/Y
(dirty compare, `3582` seeds `0x55AA`); `[0x2DE4]`=column ring index (0..0x13=19), `[0x2DE6]`=row ring index
(wrap 0xB=11 down / 0xC=12 up); `[0x6BC0]`=fine pixel scroll (0..0x10); `[0x6BF4]`=row-stride factor;
`[0x2DB6]`=scroll source offset (computed by `3569` from camera, base `0x3F40`); `[0x2DD2]/[0x2DD4]`=dest
offsets; `[0x2DD6]`=tilesheet segment; `[0x2CF1]`=level height in rows; `[0x2DF0]/[0x2DF1]`=dirty flags;
`[0x2DEE]`=accumulated tile-type flags; `0x3F40`=ring-buffer wrap base; xlat `0x6984`→`[0x6BB9]`,
`0x805A`→`[0x2DEE]`, type table `0x4DF4`→`[0x2DF0]`.

**WITNESSED + BRIDGED 2026-06-20.** `pre2/bridge/frame.py` reconstructs this as `Camera`/`ScrollState`
dataclasses + memory views (mirrors `pre2/bridge/sprites.py`); `tests/test_frame_bridge.py` (3 pass).
Witness `pre2/probes/capture_frame_state.py` (saved `artifacts/frame_state_witness/`) recorded the state
block per frame of gameplay demo 091827 and **confirmed**: `[0x2DE6] row_ring == camera_y % 12` exactly as
camera panned 0→0x21; `[0x2DF1]` counts tile-rows scrolled per frame (reset after redraw); `[0x2DDC]` carries
the `0x55AA` dirty sentinel; `[0x2DD2]/[0x2DD4]` are the two double-buffer pages (0 / 0x2000). Both ring
invariants also hold against the live snapshot.

**TileMap RESOLVED 2026-06-20** (full `346E` disasm + dump). The level segment `[0x2DD6]` holds the
row-major tile map at **base offset 0, stride 0x100 (256) bytes/row, 1 byte/tile = tile index**: `346E`'s
caller passes `ah=camera_y, al=camera_x` so `si = camera_y*256 + camera_x` (`33E9` adds `0xB00` = +11 rows
for the bottom fill row). Same segment also holds the three per-tile attribute tables `346E` xlats by tile
index: `0x6984`→`[0x6BB9]` (plane/attr), `0x805A`→`[0x2DEE]` (tile flags), `0x4DF4`→`[0x2DF0]` (type/dirty);
then `al`=tile index → `call 3B69` (recovered blit). Confirmed by dump: row 33 = `21 44 6B 21 44 1D 1E 46 7E…`
(`7E`=sky, matches the all-`7E` top rows). Modelled as `TileMap` in `pre2/bridge/frame.py` (`read_tilemap`,
reproduces the witnessed row byte-exact); `tests/test_frame_bridge.py`. **NOTE:** the three attribute
tables `346E` xlats are in the DATA segment 1A13 (the xlatb carry an `es:` override, es=1A13), NOT the
level block — and the third (`1A13:0x4DF4`) IS the same sprite-type table the blit dispatches on.

**`346E` RECOVERED + VERIFIED + WIRED 2026-06-20.** `pre2/recovered/frame_renderer.py:draw_tile_row`
recovers the 20-tile row draw; per the island-composition rule it calls the verified `blit_sprite` directly
(no ASM contact point inside the row). Contract = the four A000 planes for the row + OR-accumulated
`[0x6BB9]/[0x2DEE]/[0x2DF0]` + `di` (and other pushed regs) preserved (346E push/pops di). Verified
byte-exact vs **pure-ASM oracle** in-VM by `pre2/probes/verify_frame.py` (33 row-draws, 0 divergence) and
unit-tested (`tests/test_frame_renderer.py`). Wired hybrid + verify in `pre2/checkpoints/frame.py`
(auto-installed; 5 replacements now). Carries an `OracleLink` (1030:346E, VERIFIED). Hybrid vs ASM whole-
frame differs only by the expected speed/progress gap (native is faster), not correctness — removing the
346E hook leaves hybrid behaviour unchanged.

**`3582` RECOVERED + VERIFIED + WIRED 2026-06-20.** `pre2/recovered/frame_renderer.py:draw_grid` recovers
the full 12×20 visible-grid redraw: a prev-camera/dirty early-exit guard (`[0x2DF1]` rows-scrolled, camera
`[0x2DE0/2DE2]` vs prev `[0x2DDC/2DDE]`, `[0x2DF0]` dirty) then, on redraw, a 240-tile loop that accumulates
`tile_flags`→`[0x2DEE]` over *all* tiles but blits only **type≥1** tiles (opaque type-0 background comes from
the scroll buffer), setting `[0x2DF0]=1` if any drawn and resetting `[0x2DF1]=0`. Composes the verified blit
directly. Contract = the four A000 planes + `[0x2DEE]/[0x2DF0]/[0x2DF1]` + prev camera `[0x2DDC]/[0x2DDE]`;
`di`/regs preserved. The three attribute tables and the type/blit table are all in 1A13 (`0x805A`/`0x4DF4`).
Verified byte-exact vs pure-ASM oracle by `pre2/probes/verify_grid.py` (2 redraws, 0 divergence) + unit tests
for the decision branches (`tests/test_frame_renderer.py`); hybrid live path smoke-tested clean. Wired
hybrid+verify in `pre2/checkpoints/frame.py` (6 replacements now). `OracleLink` (1030:3582, VERIFIED).

**Task #5 plan — scroll-copy (3A08) + compositor (3B40) (characterized 2026-06-20).** The compositor
`3B40` is a **static composition**: `call 3582` (grid redraw) → `call 3A08` (scroll-copy) → `call 3035`
(panel). So per the AI-review "draw-command-stream" point, 3B40 needs no dynamic capture — once its three
sub-routines are recovered it is trivially `draw_grid(); scroll_copy(); panel()`, checkable by the (static)
call order. The real remaining work is the two **leaf pixel routines**, recovered + pixel-verified like the
blit:
- `3A08` **scroll-copy**: an EGA **write-mode-1 latched 4-plane block copy** (helper `452F` sets GC mode=1
  `out 3CE,0105` + map-mask 0x0F `out 3C4,0F02`; `451F` restores mode 0). `ds=es=0xA000`; copies the
  visible window from the scroll ring `si=[0x2DB6]` to the display page `di=[0x2DD4]`, each row split into
  `dl=0x14-[0x2DE4]` + `dh=[0x2DE4]` byte segments (the column ring split, both doubled), over `bp` rows
  computed from `0xC0 - [0x6BC0](fine) - [0x2DE6](row_ring)*16`, with a `si=0x3F40` wrap section for `bx`
  rows; then a plane-clear (`out 3C4,0F02`, `rep stosw 0`) of `[0x3A06]>>1` words at `[0x2DD4]`. Recovers as
  a 4-plane copy (cf. `renderer.restore_background`) + clear; verify pixel-level vs ASM.
- `3035` **panel/HUD** copy (screen→screen via `[0x2DD2]/[0x2DD4]`) — second leaf, recover next.

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:3B40` | **frame compositor** — `sti`; set dirty `[0x2DF0]=1`,`[0x2DDC]=0x55AA`; `call 3582` (redraw dirty grid) → `call 3A08` (scroll-copy window to A000) → `call 3035` (panel/HUD copy); ret. **Indirectly dispatched** (no direct CALL site) | OBSERVED | (frame entry) | — | who dispatches it (movement/tick table) |
| `1030:3582` | **dirty grid redraw** — if camera `[0x2DE0/2DE2]` == prev `[0x2DDC/2DDE]` and `[0x2DF0]==0` → skip (jmp `363C`); else redraw 12 (`ch=0xC`) × 20 (`cl=0x14`) tile grid: per tile `mov bl,es:[si]` index → flags via `[bx-0x7FA6]`(`0x805A`) & type `[bx+0x4DF4]`, `call 3B5C`(→blit). Resets `[0x2DEE]/2DF0/2DF1`, `[0x2DF2]=0x7E80`, `di=[0x2DB6]` | OBSERVED | (draw) | — | full grid stride/wrap detail |
| `1030:346E` | **tile-row draw** (incremental fill) — sets bg ptr `[0x2DF2]=di+0x7E80`; `ds=[0x2DD6]`; row of `cx=0x14`(20) tiles: `lodsb` index, 3-table xlat → per-tile flags, `call 3B69` (blit, `es=A000`); `[0x2DF2]+=2`; bg vert wrap `di≥0x5D40→-0x1E00`; `di-=0x28`/row | OBSERVED | (draw) | — | exact `si` row-ptr arithmetic |
| `1030:3344` / `338E` / `33F5` (+1) | **directional scroll-and-fill** (down/up/left/+right) — adjust camera `[0x2DE0/2DE2]`, fine `[0x6BC0]` (wrap 0x10), ring idx `[0x2DE4/2DE6]`; `call 3569` (recompute scroll src) then `call 346E` to fill the newly-exposed row/col. Return CF=clear if scrolled / CF=set at level edge (`[0x2CF1]-0xB` clamp). **Indirectly dispatched** | OBSERVED | (scroll) | — | the dispatch table / 4th (right) routine entry |
| `1030:3569` | **calc scroll source** — `[0x2DB6]` = f(camera col `ax`, `[0x2DE6]`, base `0x3F40`) | OBSERVED | (helper) | — | — |
| `1030:3A08` | **scroll copy** — `si=[0x2DB6]`,`di=[0x2DD4]`; planar `rep movsb` rows (split `dl`+`dh` halves around `0x3F40` wrap) into `es=A000`; then SC plane-mask `out 3C4,0F02` + zero-fill the newly exposed strip. `452F`/`451F` = EGA SC/GC save/restore | OBSERVED | (present) | — | — |
| `1030:3035` | **panel/HUD copy** — screen-to-screen (`ds=es=A000`) copy via `[0x2DD2]/[0x2DD4]`, 0x14-wide × 0xB0-tall band stepping `di` by 4 (calls `452F`/`451F`/`44C1`) | GUESS | (draw) | — | exact purpose (HUD vs split-screen) |
| tables `0x6984`→`[0x6BB9]` / `0x805A`→`[0x2DEE]` / `0x4DF4`(type)→`[0x2DF0]`; tilemap `es:si` | tile→flag xlat tables + tilemap data | OBSERVED | data | — | tilemap layout/encoding |

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:3B69` | sprite **blit dispatcher** — `idx`→`bx`; dispatch on `[0x4DF4+idx]`. Exit contract: `di+=2` (next tile column), `bx/cx/dx/si/ds` preserved. Entry `3B58` adds `di+=0x3F40`, `es=ds=0xA000` | **VERIFIED** | replacement | `pre2/recovered/renderer.py` + `pre2/replacements.py`; `tests/test_blit_renderer.py`; in-VM lockstep `pre2/probes/verify_blit.py` (1002 blits, all 3 paths, 0 divergence) + hybrid renders level 1 correctly | — |
| `1030:3B7C`–`3BD6` | **plain blit** (type 0, opaque) — unrolled `movsb` ×2/row from cache `0x5E80+idx*0x20`, stride 0x28; `sub di,0x258` resets row advance so exit `di=entry+2` | **VERIFIED** | replacement | same | — |
| `1030:3BD7`–`3D64` | **masked blit** (type≥2, partial) — `call 3D65` (restore bg), then 2-phase over 16 rows (stride `0x28`): **phase 1** (`3C1F`) `screen AND= mask` (GC func=AND `out 3CE,0x803` + map-mask 0x0F + `xchg` to load latches), mask words `[0x2DF4+(id-2)*0x20]`; **phase 2** (`3CFB`) `screen OR= sprite` plane-by-plane (read-map-select `out 3CF,cl` + map-mask `out 3C5,ch`), `lodsw`/`or [di]` from cache. Net `screen=(screen AND mask) OR sprite`. EGA state saved/restored via `451F`/`452F`. Type 1 = `call 3D65` + ret (empty) | **VERIFIED** | replacement | same | — |
| `1030:3D65` | **background restore** — copy 2B/row (4-plane latch copy) from bg buffer `[0x2DF2] - 0x28*[0x6BC0]` into the sprite rows, source linear, dest stride `0x28` with vertical **wrap** `di≥0x5D40 → di-=0x1E00` (circular bg, 0x1E00=192 rows) | **VERIFIED** | replacement | same (type 1 path + masked phase 0) | — |
| `1030:3B75` | per-sprite **type dispatch** on `[0x4DF4+idx]` (0=plain / 1=solid / ≥2=masked) — the classifier `4213` output; masked path also reads the compacted sprite bytes the classifier saved at `[0x2DF4+id*0x20]` | OBSERVED | (branch) | — | depends on classifier `4213` (task #7) |
| `1030:3A60`–`3AAB` | **background scroll/copy** (VRAM→VRAM `rep movsb`, off-screen→visible) | OBSERVED | (draw) | — | exact scroll geometry |
| `1030:42F7` | **sprite decode (local bank)** — demux 256 slots into planar cache `0x5E80`; `code<0x100` → 4 planes×32B from `sheet[0x200+code*128]` via map-mask. Side effects: `[0x2CF1]=mult`, `[0x2871]=src_seg`, copy index table → `[0x25CA]`. Exit contract: `si=0x200+0x80*nlocal`, `ds=src_seg`. RET `4369` | **VERIFIED** | replacement | `pre2/recovered/sprite_decode.py` + `pre2/bridge/sprites.py` + `pre2/replacements.py`; `tests/test_sprite_decode.py`; in-VM lockstep `pre2/probes/verify_sprite_decode.py` (native==ASM, hybrid cache byte-exact 211 slots) | src-seg `[0x2DD6]+([[0x2D86]+0x2D2C]<<4)` confirmed |
| `1030:436A` | **sprite decode (shared/union bank)** — same demux for **all** `code>=0x100` (no upper bound), source seg `((code-0x100)*8 + [0x2DD8]) & 0xFFFF` (segment arith, wraps); index from `[0x25CA]` copy. `code==0xFFFF` = unused-slot sentinel → wrapped garbage (never blitted). RET `43B2` | **VERIFIED** | replacement | same test/probe (182 in-bank shared slots byte-exact; sentinel reproduced live from VM mem) | `[0x2DD8]` bank loaded by `1030:047A` |
| `1030:3F00` | **sprite-load parent** — calls `1068`(decompress sheet→`[0x2DD6]`) → `42F7` → `047A`(load shared bank→`[0x2DD8]`, decompresses UNION) → `436A`; manages `[0x2871]` save/restore around the pair | OBSERVED | (caller) | — | `[0x2871]` reused as both SQZ bump and sprite src-seg scratch |
| `1030:4213` | **sprite classifier** — reads each 32B cache slot in **EGA read mode 1 (color compare, cmp=0, don't-care=0x0F → mask byte = `~(p0\|p1\|p2\|p3)`, bit=1 where pixel==color 0)**, set via `out 3CE,0x0805`. `dh=OR`, `dl=AND` over 0x20 mask bytes → type `[0x4DF4+idx]`: `dh==0` (no transparent px) = **0 opaque** (plain blit); `dl==0xFF` (all transparent) = **1 empty** (draw nothing); else = **id** `++[0x2DEF]` (counter starts at 1, first partial=2). Partial sprites' mask bytes saved compacted at `[0x2DF4+(id-2)*0x20]` (blit's mask source) | **VERIFIED** | (metadata) | reproduced byte-exact from the load-time witness cache (256 slots: 168 opaque / 1 empty / 87 partial) | — |
| asset `[0x000..0x200]` | sprite **index table** — 256× u16 `code` per slot | OBSERVED | data | — | — |
| asset `[0x200..]` | sprite **pixel data** — 128B/sprite = 4 planes × 32B (16×16, 1bpp/plane) | OBSERVED | data | — | — |
| `0xA000:0x5E80` | **VRAM sprite cache** — 256 slots × 32B, planar (4 planes overlaid via map mask) | OBSERVED | data | — | total slot count beyond 256? |
| `1A13:0x4DF4` | sprite **type table** — 256B, one class byte per sprite | OBSERVED | data | — | — |
| `1A13:0x25CA` | copy of the asset index table (used by shared-bank decode `436A`) | OBSERVED | data | — | — |
| `1A13:0x2DD6` / `0x2DD8` | local / shared sprite-asset base segment | OBSERVED | data | — | set by loader |

**Verification witness (important):** the mid-gameplay snapshot (`artifacts/lvl1_snap`)
is **not** a faithful witness for this island. The source sprite asset RAM is
freed/reused by then (`[0x2DD6]→5FD5` holds an all-zero index table), and the VRAM
cache at `0x5E80` is **over-drawn** during gameplay (it overlaps the draw region:
visible `0..0x1F40`, off-screen `+0x3F40`, cache `0x5E80`). Reproducing the
classifier `4213` from the snapshot cache gives 255 non-zero vs the data-segment
type table's 168 zeros / 88 ids — the table (in data seg `1A13`, intact) is the
load-time truth; the cache is stale. **To verify decode/classify, capture at level
load** (hook `42F7`/`4213` with the asset live), not from a gameplay snapshot.

## Tooling notes

- Disassembly truth: use **capstone on dumped bytes**, not the VM trace
  disassembler (it mis-computes some `Jcc` targets).
- Oracle capture: single-step the original to a routine's `RET`, not for a fixed
  instruction budget (post-routine code can overwrite output before you read it).
- A guessed invariant is not a verifier: a "decode length == header size" contract
  once falsely condemned a *correct* LZSS decoder (the header field is the output
  *reservation*, not the decode length). The authority is the lockstep vs ASM.
