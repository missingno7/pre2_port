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

## Sprite/tile render island (in progress — static disasm, not yet lockstep-verified)

Per-frame draw is VRAM→VRAM (sprites pre-decoded into a planar VRAM cache at
load); the clean recovery target is the upstream **decode** (asset → planar
cache), a pure plane-demux transform.

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:3B69` | sprite **blit** — copy one 16×16 slot (32B) from VRAM cache `0xA000:0x5E80+idx*0x20` to screen `es:di`, 2B/row, dest stride `bp=0x26`; entry `3B58` adds `di+=0x3F40`, `es=0xA000` | OBSERVED | (draw) | — | masking? plane handling on the movsb; 2nd path `3BD7` |
| `1030:3B75` | per-sprite **type dispatch** `cmp [bx+0x4DF4],1; jae 3BD7` (plain vs alt blit) | OBSERVED | (branch) | — | what `3BD7` path does (masked/large?) |
| `1030:3A60`–`3AAB` | **background scroll/copy** (VRAM→VRAM `rep movsb`, off-screen→visible) | OBSERVED | (draw) | — | exact scroll geometry |
| `1030:42F7` | **sprite decode (local bank)** — demux 256 slots into planar cache `0x5E80`; `code<0x100` → 4 planes×32B from `sheet[0x200+code*128]` via map-mask. Side effects: `[0x2CF1]=mult`, `[0x2871]=src_seg`, copy index table → `[0x25CA]`. Exit contract: `si=0x200+0x80*nlocal`, `ds=src_seg`. RET `4369` | **VERIFIED** | replacement | `pre2/recovered/sprite_decode.py` + `pre2/bridge/sprites.py` + `pre2/replacements.py`; `tests/test_sprite_decode.py`; in-VM lockstep `pre2/probes/verify_sprite_decode.py` (native==ASM, hybrid cache byte-exact 211 slots) | src-seg `[0x2DD6]+([[0x2D86]+0x2D2C]<<4)` confirmed |
| `1030:436A` | **sprite decode (shared/union bank)** — same demux for **all** `code>=0x100` (no upper bound), source seg `((code-0x100)*8 + [0x2DD8]) & 0xFFFF` (segment arith, wraps); index from `[0x25CA]` copy. `code==0xFFFF` = unused-slot sentinel → wrapped garbage (never blitted). RET `43B2` | **VERIFIED** | replacement | same test/probe (182 in-bank shared slots byte-exact; sentinel reproduced live from VM mem) | `[0x2DD8]` bank loaded by `1030:047A` |
| `1030:3F00` | **sprite-load parent** — calls `1068`(decompress sheet→`[0x2DD6]`) → `42F7` → `047A`(load shared bank→`[0x2DD8]`, decompresses UNION) → `436A`; manages `[0x2871]` save/restore around the pair | OBSERVED | (caller) | — | `[0x2871]` reused as both SQZ bump and sprite src-seg scratch |
| `1030:4213` | **sprite classifier** — AND/OR-reduce each 32B cache slot → type table `[0x4DF4+idx]` (0=empty / 1=solid-0xFF / else incrementing id `[0x2DEF]`) | OBSERVED | (metadata) | — | **read-plane mystery**: runs after both decodes (instr order confirmed), but the type table (168 zero/88 id) does NOT match a raw plane-0 reduction of the cache it scans (slot 1 has all 4 planes non-zero yet type 0) — the `es:[si]` reads must go through an EGA read-plane/read-mode, not raw plane 0. Secondary; revisit. |
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
