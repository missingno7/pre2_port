# PRE2 symbol / address ledger

Important original `PRE2.EXE` addresses, continuation points, and state locations
used by recovered code. The original binary/ASM is the oracle; entries here are
*candidates* until a verifier proves them.

Addresses are in the unpacked image (post-LZEXE); segment `1030` is the main game
code segment in the current VM layout, `1A13` is a fixed data segment.

**Columns:** location · name · confidence · role · verifier/test coverage · known unknowns.
**Confidence:** GUESS · OBSERVED · ASM_MATCHED · VERIFIED · CANONICAL.
**Role:** probe · checkpoint/verifier · replacement · data · canonical.

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
