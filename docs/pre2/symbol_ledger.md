# PRE2 symbol / address ledger

Important original `PRE2.EXE` addresses, continuation points, and state locations
used by recovered code. The original binary/ASM is the oracle; entries here are
*candidates* until a verifier proves them.

Addresses are in the self-unpacked image of the GOG `PRE2.EXE`; segment `1030` is
the main game code segment, `1A0F` is the fixed data segment.

> **Status:** the recovered/verified islands and their live boundaries are
> authoritative in code and in the generated
> [`recovered_islands.md`](recovered_islands.md). Some lower-confidence
> (`OBSERVED`/`GUESS`) rows below still carry addresses from the original recovery
> layout and are being re-derived against this binary — trust the manifest and
> `--verify-hooks` over an unverified row here.

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

### Audio mixer — the software decode/mix island (Layer 4: RECOVERED + VERIFIED 2026-06-21)

This is the **game-side** PCM mixer that fills the SB DMA buffer (distinct from the SB *hardware* above,
which is done in `dos_re`). A clean DSP island, NOT gameplay logic.

**RECOVERED + VERIFIED 2026-06-21** → `pre2/recovered/mixer.py` (`mix_channel` = 218F, `mix_sfx`, `mix_block`)
+ `pre2/bridge/audio.py` (ChannelState/Instrument/Sfx). Verified byte-exact vs the ASM in-VM:
`pre2/probes/verify_mixer.py` (218F per-channel: 200 mixes, 0 div) and `pre2/probes/verify_mixer_block.py`
(full SFX + 4-channel block: 120 blocks, complete 168-byte PCM + channel/SFX state, 0 div); unit tests
`tests/test_audio_mixer.py`. `OracleLink` (1030:218F, VERIFIED). NOTE the volume row is **`volume << 6`**
(×64, six shifts — a truncated disasm earlier showed five).

**WIRED LIVE 2026-06-21** (`pre2/checkpoints/audio.py`): the per-channel mixer `218F` is replaced by the
recovered `mix_channel` in hybrid play, so the audio you hear in `--view` is mixed by recovered code (it
reads the channel state via the bridge, mixes into the live DMA block, writes the channel state back, near-
rets; the caller reloads its regs and 218F's SP-scratch is its own). Smoke-tested: cold-boot streams audio
with the live mixer (10192 mix calls, 428 KB PCM), no crash; each block byte-exact vs ASM (verify probes).
Verify-mode (`--verify-hooks`) diffs it at the 2219 RET. The ASM ISR/SFX/tracker + SB hardware still run the
rest. Remaining for a fully recovered AudioSystem (Layer 5 detach): driving audio without the ASM driver.

**Layer 3 — tracker/sequencer `1030:227C` RECOVERED + VERIFIED + WIRED LIVE 2026-06-21**
→ `pre2/recovered/tracker.py` (`tracker_tick` = 227C, `_process_note` = 22AF, `_apply_effect`) + bridge
readers/writers in `pre2/bridge/audio.py` (PlaybackState/TrackerVoice/TrackerInstrument). One sequencer
tick: per-channel volume slides (`[+0xBC0]` delta → `[+0xBB8]` clamped 0..0x40), tick countdown `[0xB83]`,
and every `speed` `[0xB82]` ticks process the current pattern row's 4 channels via `22AF` (decode the 4-byte
in-memory cell: `sample=(b2>>4)|(b1&0x10)` triggers the note → pos=0/end=instr length `[idx*16+0xBD0]`/vol=
default `[+0xBD2]`/frac=0; `period=(b0|b1<<8)&0x7FFF` → step via table `[0xEB9]`; effect via jump table
`[0xB62]`), then advance row `[0xB86]` / order `[0xDC5]` (wrap when next > `SONG_LENGTH [0xDC0]`, the last
order *index*). Only 5 effects are used: A vol-slide, B pos-jump, C set-vol, D pattern-break, F set-speed
(0–9/E are no-ops). Verified byte-exact in-VM by `pre2/probes/verify_tracker.py` (lockstep at 227C: 300
ticks / 43 rows processed, 0 div over PlaybackState + 4 voices); unit tests `tests/test_audio_tracker.py`.
`OracleLink` (1030:227C, VERIFIED). Wired live at `pre2/checkpoints/tracker.py` (replaces 227C, verify-exit
at the 22AD RET). The SB hardware + ASM mixer ISR still run the output side.
SUBTLETY (fixed 2026-06-21): the 4 channels of a row are read from a **row pointer latched before the
channel loop** — a `Bxx`/`Dxx` on an early channel sets `[0xB86]`/`[0xB84]` for the *advance* step but must
NOT move where the rest of the row reads from (the ASM walks `si += 4` from a fixed row base). Using the
mutated `pb.row` per channel made `pattern[0xFFFF*16:]` empty → IndexError inside the audio ISR, so `INT 08h`
never returned and the timer counter `[1A13:27EA]` froze, hanging the "wait 3 ticks" loop at `1C52` while SB
DMA kept streaming (music played on) — i.e. the level-end tally screen never advanced to the map. Regression:
`tests/test_audio_tracker.py::test_pos_jump_on_early_channel_does_not_corrupt_later_channels`.

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:2029` | **SB IRQ7 ISR / block service.** Acks IRQ (`in al, cs:[0x26C]`=base+0xE), `inc [0xB5B]` block counter, reads mode `cs:[0x1D49]`; then the play routine, then mixes the *next* buffer, then PIC EOI (`out 0x20,0x20`) | OBSERVED | (ISR) | cold-boot probe (594 playback blocks @8403) | the sequencer `227C` (mode-gated, `test cs:[3],0x40`) |
| `1030:2048`–`209C` | **double-buffered DMA play** — swaps descriptors `[0x10BD/BF]`↔`[0x10C1/C3]`, programs 8237 ch1 (page `[bx]`, addr `[bx+1]`, count `0xA7`=167→168B), `write_dsp(0x14)`+len | OBSERVED | (output) | — | — |
| `1030:1C71` | `write_dsp(al)` — wait `base+0xC` bit7 clear, `out base+0xC,al` | OBSERVED | (helper) | — | — |
| `1030:20AB`–`20F3` | **SFX mix** — `rep movsw` copy active sample (`[0x1002]` src ptr / `[0x1004]` remaining len / seg `[0xB57]`) into buffer `[0x10C1]`, pad rest with silence (0); no SFX → whole block 0 | OBSERVED | (mix) | — | how `[0x1002/1004]` are armed (SFX trigger) |
| `1030:218F` | **per-channel MOD mixer** (called 4× from `20FE`-`2119`, `bx=ch*2`). Resample+volume+additive: `lodsb` sample (es:si = instrument far ptr `[idx*16+0xBD8]` + pos) → `xlatb` volume table `0x12BD` → `add [di],al` into the 168B block; advance pos by fractional **period** `[+0xBA8]` via accumulator `[+0xBC8]`; loop/end via `[idx*16+0xBD4]` loop-start / `[+0xBD6]` loop-len | OBSERVED | (mix kernel) | — | exact period→step + volume-table layout |
| per-channel state (ds=1A13, `ch*2`) | `[+0xB88]`=sample pos (`0xFFFF`=off), `[+0xB90]`=length/end, `[+0xB98]`=instrument idx, `[+0xBA8]`=period/step, `[+0xBC8]`=frac accumulator; instrument table `0xBD8` stride 16 (far ptr/loop start/loop len); volume tables `0x12BD` | OBSERVED | data | — | channel count = 4; sample-rate fixed 8403 |

**Recover (renderer-of-audio):** `pre2/bridge/audio.py` (channel state / instrument table / SFX state /
buffers) → `pre2/recovered/audio_mixer.py` (`mix_channel` = 218F, `mix_sfx`, `mix_block` = SFX + 4 channels)
→ thin checkpoint at the ISR's mix section → **verify by 168-byte PCM-block diff vs the ASM**. The `.TRK`
module SQZ decompression is already recovered (`unpack_sqz`); the sequencer `227C` (pattern/row advance) is
the music-logic layer, recovered later.

## SQZ decompressor (first recovered island → merges into the asset loader)

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:107B` | `sqz_decompress` — decompressor public entry (opens file at `1A13:DX`, dispatches, decodes, returns `ax`=out seg) | VERIFIED | replacement | `pre2/replacements.py`; `--verify-hooks`; `tests/test_sqz_codec.py` | — |
| `1030:00EC` / `00EF` | caller `call 107B` / return (`mov [3b],ax`) | ASM_MATCHED | (caller) | hook near-rets here via `cpu.pop()` | other callers (KEYB returns elsewhere) |
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

The sprite-sheet **decode** sub-island (`4316`/`4389`) is DONE/VERIFIED (above).
The current sub-island is the **draw primitive layer**.

**Island scope (as shipped):**
- **RECOVERED + VERIFIED:** the blit primitive `3B88` + its paths (`3B7C` plain /
  `3BD7` masked / solid) + `3D65` bg-restore → `pre2/recovered/renderer.py:blit_sprite`.
- **RECOVERED + ASM_MATCHED:** the classifier `4232` — the producer of the blit's
  `[0x4DF8]` type table + `[0x2DF8]` compacted-mask buffer →
  `pre2/recovered/sprite_classify.py:classify_sprites`. Byte-exact vs the load-time
  ASM witness (type table 256/256: opaque 168 / empty 1 / partial 87) +
  mask cross-check vs the blit witness (`tests/test_sprite_classify.py`). Verify-mode
  lockstep wired (`pre2/checkpoints/sprite_classify.py`); NOT yet a live hybrid
  replacement / VERIFIED — that needs a menu→level-load demo to pin the EGA read-mode-1
  register exit contract (the gameplay demos start past `4232`). With this, the
  renderer's sprite pipeline is recovered end to end: **decode → classify → blit**.
- **UPPER boundary (OUT — the NEXT island):** the tilemap / sprite-list **draw
  loops** (`34A0`, `3552`, callers of `3B58` at `65A0`/`8BFF`) that iterate game
  state (tilemap layout, object list, scroll position), build per-entry flags via
  `xlatb`, compute screen offsets, and call the blit. Also the background
  scroll/compose (`3A60`/`3A27` frame orchestrator). These own the game data model.
- **LOWER boundary:** EGA/VRAM hardware (the VM provides it).
- **Verification unit:** one blit call — inputs `(idx, screen_off, es, cache,
  [0x2DF4], bg buffer, GC/map-mask state)` → framebuffer delta (note the masked
  path's `xchg` also writes `[0x2DF4]`, a read/write contract).

### Object-list draw island (boundary mapping started 2026-06-20)

The moving-sprite / object draw path (renderer-facing; NOT gameplay update yet).

- **`1030:653D` — per-object sprite draw (the draw-command unit). RECOVERED, PENDING VERIFICATION.**
  Recovered as `pre2/recovered/object_draw.py:draw_object_sprite` (composes the verified `blit_sprite`),
  bridge `pre2/bridge/objects.py`, verify probe `pre2/probes/verify_object.py`. **Not yet verified / not
  wired:** demo 091827 never reaches 653D (0 calls in 3000 frames — its objects don't appear), same
  test-data gap as `3B40`. Needs a demo that actually draws objects. Input `di` = object
  tile position (`dh`=row, `dl`=col), sprite index in `al`. Culls against the camera window:
  `dl-[0x2DE0] >= 0x14` (20 cols) or `dh-[0x2DE2] >= 0xC` (12 rows) → not drawn (RET CF=set).
  Else computes the screen dest offset from the tile position (`row%12`·`0x50`·16 + `(col%20)<<bh`;
  the `0x50/2` vs `0x28/1` stride/shift pair is chosen by `cs:[1]` mode) and calls the blit
  wrapper `3B58` (→ recovered blit). Sets `[0x6BB9]=1`. **This is the natural `ObjectSpriteCommand`
  unit and composes the recovered `blit_sprite`.**
- **`~1030:5406` — object-table draw loop (multi-tile structures).** Walks an object table at
  offset **`0x83EF`, 15 slots × 10 bytes**. Per-slot record: `[+0]` word draw position (decremented
  over time), `[+2]` byte `dl` (width, tiles), `[+3]` byte `dh` (height, tiles), `[+4]` word
  id/proximity key (`0xFFFF`=empty slot, `0xFFFE`=triggered), `[+6]` word data pointer (sprite
  bytes, read from seg `[0x2871]`), `[+8]` (2 bytes, TBD). A proximity pre-pass (`|key-dx|<=8` →
  mark `0xFFFE`, set `[0x6BE6]=7`) then draws each object as a `dl×dh` block of tiles (per cell:
  read/shift a tile from level seg `[0x2DD6]`, `call 653D`). Calls `653D` at `5463`/`548C`.
- **`1030:5C9E`** — NOT a clean draw list. It sits inside **object-update handler dispatch**
  (`5C40`: `call [bx*2 + 0x7DA5]` — a per-object-type function-pointer table — with collision/position
  probing on `[0x4F18/4F1A/4F1E]`). The helper at `5C8B` is "if the sprite type is opaque (type 0) draw
  it via `653D`, else mark `[0x2DF0]=1` + `[0x2DDC]=0x55AA` so the grid redraw handles it." So the moving
  **player/enemy sprites draw themselves inside their update handlers** = gameplay logic. **Deferred to the
  object-update island** (do not recover here).
- **Known unknowns:** the object-table *segment* (the `[si]` reads use entry `ds`); the `[+8]`
  field; whether `5C9E` is the player/enemy list; draw order across the lists.
- **Plan (renderer-first, refined after `5C9E`):** the renderer-facing object draw = the `653D`
  primitive + the `0x83EF` structure loop; the player/enemy self-draw is entangled with update and is
  deferred. Recover `653D` first (clean, shared draw-command unit) → `pre2/recovered/object_draw.py:
  draw_object_sprite` composing the recovered `blit_sprite`, with a factual `pre2/bridge/objects.py`
  (camera/mode inputs; later the `0x83EF` `ObjectSlot` table). Verify by draw-command/contract lockstep
  vs the ASM, not by re-proving pixels (the blit is already verified).

### `1030:26FA` — the HOT moving-sprite renderer (OBSERVED 2026-06-21, profiled #1 perf gap)
**This, not `653D`, is the dominant gameplay sprite path** (~78% of interpreted instr; `653D` is
dormant). Entry `26FA` (prologue `push ax,bx,cx,dx,di,si,ds,es,bp`), single ret `2DF9`; called from
~12 sites (024D, 32C5, 4D84…). Iterates an **active-sprite list** `[0x4F0A .. 0x5720]`, **18-byte
records**, walked top→down (`si=[2DEE]`, `-=0x12` each, stop `< 0x4F0A`). Record fields seen:
`[+0]` world X (word), `[+2]` world Y (word), `[+4]` sprite id (word, `-1`=empty), `[+5]` flags
(bit5 set=drawn; H-flip from id bit via `rcl cs:[26E2]`), `[+0x11]` anim/life counter (dec+blink-gated
by `[6BD5]&3`). Per-sprite attribute tables indexed by `id<<1`: `[0x7190]` width (word), `[0x752A/B]`
x/y draw offsets, **`[0x62E8]` sprite-data SEGMENT**, **`[0x5F48]` sprite-data OFFSET**. Computes
camera-relative screen X/Y (`X=[+0]-[2DE4]*16`, `Y=[+2]-[2DE6]*16-[6BC4]`), 4-way culls (X≥0x140,
X+w<0, Y≥0xB0, Y<…) and clips (left/right/top/bottom → adjust byte-width `[26E4]`, rows `[26EC]`,
source-skip, dest `[26F1]`). Dest VRAM off = `screenX>>3 + [2DD8]` (display page). Calls `452B`
(ensure sprite decoded) then blits. **Contract = A000 planar VRAM + object-record mutations
(`[+5]` flags) + cursor `[2DEE]` + scratch `cs:[26E0..26F7]`.**
- **Blit = GC-assisted planar, plane loop** (map-mask `1,2,4,8` via seq `0x3C5`; per-plane src stride
  `[26E0]`; row stride via `bp = byte_width - 0x28`). Modes: **opaque byte-aligned** copy (`2C2A`:
  `lodsb/scasb(latch)/stosb` per plane); **shifted+masked** (`2C4F`/`2C70`: software shift-carry
  `ah`=carry, `ch`=`0xFF<<shift` edge mask, `bh`=~ch, with GC func=OR via `0x3CE` idx3=0x10 +
  `not al`); **H-flipped** reverse copy (`2915`: `bx=0x2F34`, `std`, entry `2BC3` vs flipped); plus a
  **clipped** variant `2CEA` (when `cs:[26E3]`!=0). Recover on de-interleaved plane buffers (like
  `renderer.py`) so the latch dance collapses to per-plane writes; model the shift-carry + GC
  func per mode; verify byte-exact via lockstep at `2DF9`.
- **Phase A (driver) + Phase B (blit) RECOVERED + VERIFIED byte-exact (2026-06-21)** →
  `pre2/recovered/object_render.py` (`plan_sprite`/`plan_frame`/`paint_sprite`/`_phase`),
  bridge `pre2/bridge/object_render.py`, committed test `tests/test_object_render.py`.
  Driver (cull/position/clip) 120/120 + blit 0 divergent bytes (all modes, shifts 0-7,
  left/right clip incl byte_width==0 sliver, AND **H-flip** via bit-reverse table CS:0x2F34).
  **LIVE-HOOKED** at `pre2/checkpoints/object_render.py` (verify-mode 0 divergence) →
  **2.26x gameplay speedup** (18.8 vs 8.3 game-frames/s). Committed test test_object_render.py
  (+ clipflip + topclip fixtures). Clipped+flipped (2AA1) recovered (unified `_phase_flip`);
  top/bottom-clip fixed (source plane-block stride = `full_rows*src_bw`, NOT clipped rows —
  source holds full-height sprites). Final audit 0 bad bytes across 3 snapshots (incl 69
  top-clips, clip-flips). Only the special HUD sprite id 0x135 (`2784` no-camera path) is
  guarded (never observed) → Pre2HybridGap if it ever appears.
- **Phase B (the blit) — full spec mapped, TODO implement.** Per sprite it is a **two-phase
  composite** (like the verified `blit_masked`): a **blink/anim mode** `[26F7]` chooses the
  phases — set at `2740..2761`: default `1`; while life `[si+0x11]`>0 it blinks (`[6BD5]&3`
  → `0`=erase 3/4 frames, `1`=draw 1/4); when life hits 0 it draws (`1`, or `0x10` if id
  bit14). `0`→AND-only (erase), `1`→AND+OR, `0x10`→OR-only. **AND-mask phase** (`2C4F` shifted /
  `2BDD` aligned): all planes `&= ror8(~src, shift)` (GC func=AND @28DE). **OR-sprite phase**
  (`2C70`/`2C00`): per plane (map-mask 1/2/4/8) `|= ror8(src, shift)` (GC func=OR), source =
  4 sequential plane blocks of `byte_width*rows` (+ a leading mask block for the AND phase),
  per-plane advance via `[26E0]`; software carry `ah`=spill, `ch`=`0xFF<<shift`, `bh`=`~ch`
  handles the byte-boundary, trailing `xchg [di],ah` writes the final spill byte. **Clipped
  variant** `2CEA` (when `[26E3]` set by left/right clip): left-skip `[26ED]`, right partial
  `[26EF]`/`[26F0]`. Recovery approach: faithful VGA-op model on plane buffers —
  `plane[p][o] = func(ror8(V,shift), plane[p][o])` for map-masked planes; also mutate life
  `[si+0x11]` (contract). Verify: lockstep at `26FA` diffing all 4 planes + record mutations.

### NOTE — missed frame-renderer leaf `1030:34ED` (tile-column fill)
`34ED` is the **vertical tile-column fill** — the horizontal-scroll counterpart to `348D`'s
20-tile row fill: same 3-table xlat + blit, but a 12-tile **column** (`cx=0xC`), `si += 0x100`/tile,
`di += 0x27E`/tile, `[0x2DF2] += 0x40`/tile. It is a frame-renderer leaf we have NOT recovered yet
(a quick sibling of the recovered `draw_tile_row`); recover it to complete the tile-fill pair.

### Frame renderer / scroll engine (boundary MAPPED 2026-06-20)

**Merge target:** these routines are one landmass = the **frame renderer + scroll engine**. They
recover into a `pre2/recovered/frame_renderer.py` driven by a `Camera`/`ScrollState`/`TileMap` model in
`pre2/bridge/` — NOT a pile of per-routine hooks. Coastline note: the per-frame entry (`3B40`) and the
directional scroll routines (`3344/338E/33F5/…`) have **no direct callers in the code segment — they are
dispatched indirectly** (movement/direction dispatch; cf. `3300: call dx`). So the eventual checkpoint is a
**semantic frame/tick contract** (camera + dirty + framebuffer), not a static CALL hook. Capture witness +
build the Camera/TileMap bridge first (task #2); wire one thin frame-boundary adapter, not one-per-routine.

**Game data model (ds=1A13) — the emerging `Camera`/`ScrollState`/`TileMap`:**
`[0x2DE0]`=camera X (tile col), `[0x2DE2]`=camera Y (tile row); `[0x2DDC]/[0x2DDE]`=previous camera X/Y
(dirty compare, `35A1` seeds `0x55AA`); `[0x2DE4]`=column ring index (0..0x13=19), `[0x2DE6]`=row ring index
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

**TileMap RESOLVED 2026-06-20** (full `348D` disasm + dump). The level segment `[0x2DD6]` holds the
row-major tile map at **base offset 0, stride 0x100 (256) bytes/row, 1 byte/tile = tile index**: `348D`'s
caller passes `ah=camera_y, al=camera_x` so `si = camera_y*256 + camera_x` (`33E9` adds `0xB00` = +11 rows
for the bottom fill row). Same segment also holds the three per-tile attribute tables `348D` xlats by tile
index: `0x6984`→`[0x6BB9]` (plane/attr), `0x805A`→`[0x2DEE]` (tile flags), `0x4DF4`→`[0x2DF0]` (type/dirty);
then `al`=tile index → `call 3B88` (recovered blit). Confirmed by dump: row 33 = `21 44 6B 21 44 1D 1E 46 7E…`
(`7E`=sky, matches the all-`7E` top rows). Modelled as `TileMap` in `pre2/bridge/frame.py` (`read_tilemap`,
reproduces the witnessed row byte-exact); `tests/test_frame_bridge.py`. **NOTE:** the three attribute
tables `348D` xlats are in the DATA segment 1A13 (the xlatb carry an `es:` override, es=1A13), NOT the
level block — and the third (`1A13:0x4DF4`) IS the same sprite-type table the blit dispatches on.

**`348D` RECOVERED + VERIFIED + WIRED 2026-06-20.** `pre2/recovered/frame_renderer.py:draw_tile_row`
recovers the 20-tile row draw; per the island-composition rule it calls the verified `blit_sprite` directly
(no ASM contact point inside the row). Contract = the four A000 planes for the row + OR-accumulated
`[0x6BB9]/[0x2DEE]/[0x2DF0]` + `di` (and other pushed regs) preserved (348D push/pops di). Verified
byte-exact vs **pure-ASM oracle** in-VM by `pre2/probes/verify_frame.py` (33 row-draws, 0 divergence) and
unit-tested (`tests/test_frame_renderer.py`). Wired hybrid + verify in `pre2/checkpoints/frame.py`
(auto-installed; 5 replacements now). Carries an `OracleLink` (1030:348D, VERIFIED). Hybrid vs ASM whole-
frame differs only by the expected speed/progress gap (native is faster), not correctness — removing the
348D hook leaves hybrid behaviour unchanged.

**`35A1` RECOVERED + VERIFIED + WIRED 2026-06-20.** `pre2/recovered/frame_renderer.py:draw_grid` recovers
the full 12×20 visible-grid redraw: a prev-camera/dirty early-exit guard (`[0x2DF1]` rows-scrolled, camera
`[0x2DE0/2DE2]` vs prev `[0x2DDC/2DDE]`, `[0x2DF0]` dirty) then, on redraw, a 240-tile loop that accumulates
`tile_flags`→`[0x2DEE]` over *all* tiles but blits only **type≥1** tiles (opaque type-0 background comes from
the scroll buffer), setting `[0x2DF0]=1` if any drawn and resetting `[0x2DF1]=0`. Composes the verified blit
directly. Contract = the four A000 planes + `[0x2DEE]/[0x2DF0]/[0x2DF1]` + prev camera `[0x2DDC]/[0x2DDE]`;
`di`/regs preserved. The three attribute tables and the type/blit table are all in 1A13 (`0x805A`/`0x4DF4`).
Verified byte-exact vs pure-ASM oracle by `pre2/probes/verify_grid.py` (2 redraws, 0 divergence) + unit tests
for the decision branches (`tests/test_frame_renderer.py`); hybrid live path smoke-tested clean. Wired
hybrid+verify in `pre2/checkpoints/frame.py` (6 replacements now). `OracleLink` (1030:35A1, VERIFIED).

**Task #5 plan — scroll-copy (3A27) + compositor (3B40) (characterized 2026-06-20).** The compositor
`3B40` is a **static composition**: `call 35A1` (grid redraw) → `call 3A27` (scroll-copy) → `call 3054`
(panel). So per the AI-review "draw-command-stream" point, 3B40 needs no dynamic capture — once its three
sub-routines are recovered it is trivially `draw_grid(); scroll_copy(); panel()`, checkable by the (static)
call order. The real remaining work is the two **leaf pixel routines**, recovered + pixel-verified like the
blit:
- `3A27` **scroll-copy** — **RECOVERED + VERIFIED + WIRED 2026-06-20.** EGA **write-mode-1 latched 4-plane
  block copy** (helper `452F` sets GC mode=1 `out 3CE,0105` + map-mask 0x0F; `451F` restores mode 0).
  `ds=es=0xA000`; copies the visible window from the scroll ring `si=[0x2DB6]` to the display page
  `di=[0x2DD4]`, each row split into `dl=0x14-[0x2DE4]` + `dh=[0x2DE4]` byte segments (column ring, both
  doubled), over `bp` rows from `0xC0 - [0x6BC0](fine) - [0x2DE6](row_ring)*16`, with a `si=0x3F40` wrap
  section for `bx` rows; then an all-plane clear (`rep stosw 0`) of `[0x3A06]>>1` words at `[0x2DD4]`.
  `pre2/recovered/frame_renderer.py:scroll_copy` (4-plane copy + clear, cf. `renderer.restore_background`).
  Verified byte-exact vs pure-ASM oracle (`pre2/probes/verify_scroll.py`, 3 copies, 0 divergence); wired
  hybrid+verify in `pre2/checkpoints/frame.py` (7 replacements now), hybrid smoke-tested clean.
  `OracleLink` (1030:3A27, VERIFIED).
- `3054` **page-flip copy** — **RECOVERED + VERIFIED + WIRED 2026-06-20.** Double-buffer present:
  `307C` copies 2-byte × 0xB0-row vertical strips (write-mode-1 latched 4-plane copy, stride 0x28) from the
  back page `[0x2DD4]` to the front page `[0x2DD2]`, at symmetric columns `0x14±2k` for `k=0..9`, with
  `44C1` vsync waits interleaved (timing-only, no pixel contract — omitted). `frame_renderer.py:panel_copy`;
  verified vs pure-ASM (`pre2/probes/verify_panel.py`, 1 copy, 0 div); wired hybrid+verify (8 replacements).
  `OracleLink` (1030:3054, VERIFIED).
- `3B40` **compositor** — static composition `sti; [0x2DF0]=1; [0x2DDC]=0x55AA; draw_grid(); scroll_copy();
  panel(); pop es; pop ds; ret`. **NOT wired**: no available demo reaches 3B40 (its three leaves are
  exercised via their *other* callers — 0237 / 01E2 / 023A — and verified there), so a native 3B40 cannot be
  lockstep-verified yet. The hybrid already runs all three leaves natively when ASM 3B40 calls them; wire a
  native compositor once a scenario exercises 3B40. (Recorded in `pre2/checkpoints/frame.py`.)

**Task #5 status:** the compositor's pixel work — grid redraw (35A1), scroll-copy (3A27), page-flip (3054)
— is fully recovered, verified byte-exact, and live. 3B40 itself is thin glue, characterized but deferred
(unverifiable with current demos). The frame-renderer coastline is now native except that thin glue.

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:3B40` | **frame compositor** — `sti`; set dirty `[0x2DF0]=1`,`[0x2DDC]=0x55AA`; `call 35A1` (redraw dirty grid) → `call 3A27` (scroll-copy window to A000) → `call 3054` (panel/HUD copy); ret. **Indirectly dispatched** (no direct CALL site) | OBSERVED | (frame entry) | — | who dispatches it (movement/tick table) |
| `1030:35A1` | **dirty grid redraw** — if camera `[0x2DE0/2DE2]` == prev `[0x2DDC/2DDE]` and `[0x2DF0]==0` → skip (jmp `363C`); else redraw 12 (`ch=0xC`) × 20 (`cl=0x14`) tile grid: per tile `mov bl,es:[si]` index → flags via `[bx-0x7FA6]`(`0x805A`) & type `[bx+0x4DF4]`, `call 3B5C`(→blit). Resets `[0x2DEE]/2DF0/2DF1`, `[0x2DF2]=0x7E80`, `di=[0x2DB6]` | OBSERVED | (draw) | — | full grid stride/wrap detail |
| `1030:348D` | **tile-row draw** (incremental fill) — sets bg ptr `[0x2DF2]=di+0x7E80`; `ds=[0x2DD6]`; row of `cx=0x14`(20) tiles: `lodsb` index, 3-table xlat → per-tile flags, `call 3B88` (blit, `es=A000`); `[0x2DF2]+=2`; bg vert wrap `di≥0x5D40→-0x1E00`; `di-=0x28`/row | OBSERVED | (draw) | — | exact `si` row-ptr arithmetic |
| `1030:3344` / `338E` / `33F5` (+1) | **directional scroll-and-fill** (down/up/left/+right) — adjust camera `[0x2DE0/2DE2]`, fine `[0x6BC0]` (wrap 0x10), ring idx `[0x2DE4/2DE6]`; `call 3569` (recompute scroll src) then `call 348D` to fill the newly-exposed row/col. Return CF=clear if scrolled / CF=set at level edge (`[0x2CF1]-0xB` clamp). **Indirectly dispatched** | OBSERVED | (scroll) | — | the dispatch table / 4th (right) routine entry |
| `1030:3569` | **calc scroll source** — `[0x2DB6]` = f(camera col `ax`, `[0x2DE6]`, base `0x3F40`) | OBSERVED | (helper) | — | — |
| `1030:3A27` | **scroll copy** — `si=[0x2DB6]`,`di=[0x2DD4]`; planar `rep movsb` rows (split `dl`+`dh` halves around `0x3F40` wrap) into `es=A000`; then SC plane-mask `out 3C4,0F02` + zero-fill the newly exposed strip. `452F`/`451F` = EGA SC/GC save/restore | OBSERVED | (present) | — | — |
| `1030:3054` | **per-frame double-buffer page-flip** (= the room/cave-enter **curtain**) — back page `[0x2DD8]` → front `[0x2DD6]`, copying 2-byte × 0xB0-row vertical strips at symmetric cols `0x14±2k` (k=0..9) centre-outward, vsync-paced (the inner strip copy is `1030:309B`: `si+=[0x2DD8]`, `di+=[0x2DD6]`, `ds=es=A000`, `movsb`×2 then `+0x26`/row). The center-out reveal is a SUB-FRAME effect (whole copy within one call); on a black→new-room flip it IS the visible curtain. RET `309A` | **VERIFIED** | replacement (passthrough: vsync pacing is the effect) | `recovered/frame_renderer.py:panel_copy` (+`completed_pairs`); `checkpoints/frame.py:frame_panel_copy`; witness 231731 (boundary Δ=0) | — |
| tables `0x6984`→`[0x6BB9]` / `0x805A`→`[0x2DEE]` / `0x4DF4`(type)→`[0x2DF0]`; tilemap `es:si` | tile→flag xlat tables + tilemap data | OBSERVED | data | — | tilemap layout/encoding |

| Location | Name | Confidence | Role | Coverage | Known unknowns |
|---|---|---|---|---|---|
| `1030:3B88` | sprite **blit dispatcher** — `idx`→`bx`; dispatch on `[0x4DF4+idx]`. Exit contract: `di+=2` (next tile column), `bx/cx/dx/si/ds` preserved. Entry `3B58` adds `di+=0x3F40`, `es=ds=0xA000` | **VERIFIED** | replacement | `pre2/recovered/renderer.py` + `pre2/replacements.py`; `tests/test_blit_renderer.py`; in-VM lockstep `pre2/probes/verify_blit.py` (1002 blits, all 3 paths, 0 divergence) + hybrid renders level 1 correctly | — |
| `1030:3B7C`–`3BD6` | **plain blit** (type 0, opaque) — unrolled `movsb` ×2/row from cache `0x5E80+idx*0x20`, stride 0x28; `sub di,0x258` resets row advance so exit `di=entry+2` | **VERIFIED** | replacement | same | — |
| `1030:3BD7`–`3D64` | **masked blit** (type≥2, partial) — `call 3D65` (restore bg), then 2-phase over 16 rows (stride `0x28`): **phase 1** (`3C1F`) `screen AND= mask` (GC func=AND `out 3CE,0x803` + map-mask 0x0F + `xchg` to load latches), mask words `[0x2DF4+(id-2)*0x20]`; **phase 2** (`3CFB`) `screen OR= sprite` plane-by-plane (read-map-select `out 3CF,cl` + map-mask `out 3C5,ch`), `lodsw`/`or [di]` from cache. Net `screen=(screen AND mask) OR sprite`. EGA state saved/restored via `451F`/`452F`. Type 1 = `call 3D65` + ret (empty) | **VERIFIED** | replacement | same | — |
| `1030:3D65` | **background restore** — copy 2B/row (4-plane latch copy) from bg buffer `[0x2DF2] - 0x28*[0x6BC0]` into the sprite rows, source linear, dest stride `0x28` with vertical **wrap** `di≥0x5D40 → di-=0x1E00` (circular bg, 0x1E00=192 rows) | **VERIFIED** | replacement | same (type 1 path + masked phase 0) | — |
| `1030:3B75` | per-sprite **type dispatch** on `[0x4DF4+idx]` (0=plain / 1=solid / ≥2=masked) — the classifier `4232` output; masked path also reads the compacted sprite bytes the classifier saved at `[0x2DF4+id*0x20]` | OBSERVED | (branch) | — | depends on classifier `4232` (task #7) |
| `1030:3A60`–`3AAB` | **background scroll/copy** (VRAM→VRAM `rep movsb`, off-screen→visible) | OBSERVED | (draw) | — | exact scroll geometry |
| `1030:4316` | **sprite decode (local bank)** — demux 256 slots into planar cache `0x5E80`; `code<0x100` → 4 planes×32B from `sheet[0x200+code*128]` via map-mask. Side effects: `[0x2CF1]=mult`, `[0x2871]=src_seg`, copy index table → `[0x25CA]`. Exit contract: `si=0x200+0x80*nlocal`, `ds=src_seg`. RET `4369` | **VERIFIED** | replacement | `pre2/recovered/sprite_decode.py` + `pre2/bridge/sprites.py` + `pre2/replacements.py`; `tests/test_sprite_decode.py`; in-VM lockstep `pre2/probes/verify_sprite_decode.py` (native==ASM, hybrid cache byte-exact 211 slots) | src-seg `[0x2DD6]+([[0x2D86]+0x2D2C]<<4)` confirmed |
| `1030:4389` | **sprite decode (shared/union bank)** — same demux for **all** `code>=0x100` (no upper bound), source seg `((code-0x100)*8 + [0x2DD8]) & 0xFFFF` (segment arith, wraps); index from `[0x25CA]` copy. `code==0xFFFF` = unused-slot sentinel → wrapped garbage (never blitted). RET `43B2` | **VERIFIED** | replacement | same test/probe (182 in-bank shared slots byte-exact; sentinel reproduced live from VM mem) | `[0x2DD8]` bank loaded by `1030:047A` |
| `1030:3F00` | **sprite-load parent** — calls `107B`(decompress sheet→`[0x2DD6]`) → `4316` → `047A`(load shared bank→`[0x2DD8]`, decompresses UNION) → `4389`; manages `[0x2871]` save/restore around the pair | OBSERVED | (caller) | — | `[0x2871]` reused as both SQZ bump and sprite src-seg scratch |
| `1030:4580` | **static HUD bar blit** — 320×23 planar status-bar bitmap → page+`0x1B80` (`rep movsb 0x398`/plane). RET `45AA` | **VERIFIED** | replacement | `recovered/hud.py:draw_status_bar`; `tests/test_hud_chrome.py` | — |
| `1030:45B8` | **dynamic HUD draw** — incremental layout into page `[0x2DD8]`: lives digit `0x1CED` (if `[0x6CA4]` changed), 6-digit score+trailing-0 `0x1CF1` (if changed), energy hearts `0x1D01`, then BONUS letters; each glyph via `473D`. Exits via `46EB jmp 45AB` → `ret 45AD`. Dual-pages to `[0x2DD6]` when `cs:[0x45AE]!=0` | **VERIFIED** | verifier (verify-only; ASM draws live) | `recovered/hud.py:draw_hud`; `checkpoints/hud.py` (ret 45AB, fired 53×/0 div); `tests/test_hud_chrome.py` | runtime *replacement* deferred (incremental+caches+dual-page) |
| `1030:4683`→`46C5` | **BONUS-letter decision + draw** — `ah` = (`[0x6C00]` flash → `0x1F` if `[0x6BD5]&1` else `0`) else collected `[0x6CA7]`; loop glyphs `0x0C..0x10` at di table `[0x6F86]`, draw set bits | **VERIFIED** | (part of 45B8) | `recovered/hud.py:effective_bonus_mask`; grounded by `checkpoints/hud.py` | — |
| `1030:4232` | **sprite classifier** — produces the sprite type/mask tables the recovered blit primitive consumes; **RECOVERED (verify-mode wired); live replacement pending a level-load witness.** Reads each 32B cache slot in **EGA read mode 1 (color compare, cmp=0, don't-care=0x0F → mask byte = `~(p0\|p1\|p2\|p3)`, bit=1 where pixel==color 0)**, set via `out 3CE,0x0805`. `dh=OR`, `dl=AND` over 0x20 mask bytes → type `[0x4DF4+idx]`: `dh==0` (no transparent px) = **0 opaque** (plain blit); `dl==0xFF` (all transparent) = **1 empty** (draw nothing); else = **id** `++[0x2DEF]` (counter starts at 1, first partial=2). Partial sprites' mask bytes saved compacted at `[0x2DF4+(id-2)*0x20]` (blit's mask source) | **ASM_MATCHED** | (producer) | recovered → `sprite_classify.classify_sprites`; byte-exact vs the load-time witness (256 slots: 168 opaque / 1 empty / 87 partial) + mask x-check; verify-mode lockstep wired; live-replacement + VERIFIED pending a menu→level-load demo | sprite pipeline |
| asset `[0x000..0x200]` | sprite **index table** — 256× u16 `code` per slot | OBSERVED | data | — | — |
| asset `[0x200..]` | sprite **pixel data** — 128B/sprite = 4 planes × 32B (16×16, 1bpp/plane) | OBSERVED | data | — | — |
| `0xA000:0x5E80` | **VRAM sprite cache** — 256 slots × 32B, planar (4 planes overlaid via map mask) | OBSERVED | data | — | total slot count beyond 256? |
| `1A13:0x4DF4` | sprite **type table** — 256B, one class byte per sprite | OBSERVED | data | — | — |
| `1A13:0x25CA` | copy of the asset index table (used by shared-bank decode `4389`) | OBSERVED | data | — | — |
| `1A13:0x2DD6` / `0x2DD8` | local / shared sprite-asset base segment | OBSERVED | data | — | set by loader |

**Verification witness (important):** the mid-gameplay snapshot (`artifacts/lvl1_snap`)
is **not** a faithful witness for this island. The source sprite asset RAM is
freed/reused by then (`[0x2DD6]→5FD5` holds an all-zero index table), and the VRAM
cache at `0x5E80` is **over-drawn** during gameplay (it overlaps the draw region:
visible `0..0x1F40`, off-screen `+0x3F40`, cache `0x5E80`). Reproducing the
classifier `4232` from the snapshot cache gives 255 non-zero vs the data-segment
type table's 168 zeros / 88 ids — the table (in data seg `1A13`, intact) is the
load-time truth; the cache is stale. **To verify decode/classify, capture at level
load** (hook `4316`/`4232` with the asset live), not from a gameplay snapshot.

## Gameplay effect overlays + shared RNG (VERIFIED — recovered 2026-06-24)

The three effect passes that draw OVER the core `render_frame` output, plus the two
RNGs the firefly swarm (and the rest of the game) shares. All wired through
`bridge/gameplay_effects.py` (`GameplayEffects` bundle → `apply_gameplay_effects`)
and folded into `render_game_visual_state`. See the manifest for full contracts.

| location | name | conf | role | notes |
|---|---|---|---|---|
| `1030:4B8E` | point-particle draw | VERIFIED | leaf | one-shot points `[0x7DE6]` (20 slots); snapshot at entry (pre-kill). `recovered.particles.draw_particles` |
| `1030:3721`/`3732` | foreground-tile pass | VERIFIED | leaf | redraw flag-`0x40` tiles OVER sprites; selection walks active list `[0x4F0A]` stride 0x12; `recovered.foreground_tiles` |
| `1030:37F7` | masked tile blit | VERIFIED | leaf | color-0-keyed transparent blit; gfx seg `word[0x003b]` : `word[0x8167+tile*2]<<7`, plane-major; phase1 AND ~footprint, phase2 OR |
| `1030:54AB` | firefly swarm | VERIFIED | leaf + **replacement** | persistent 20-slot swarm `[0x6EA9]` (stride 8, `0x55AA`=dead). `draw_fireflies` (draw) + `firefly_sim.step_fireflies` (full pass, live replacement `checkpoints/fireflies.py`) |
| `1030:26CF` | RNG-A (LCG) | VERIFIED | data/leaf | 16-bit seed `[0x28C1]`: `s = ror((s+0x9248), 3)`; returns low byte. SHARED across the game |
| `1030:39DF` | RNG-B (4-byte) | VERIFIED | data/leaf | state word `[0x2CEF]` + bytes `[0x2CEC]`/`[0x2CED]`/`[0x2CEE]`; returns `[0x2CED]`. SHARED |
| `1030:452B` | GC reset helper | OBSERVED | data | sets GC mode(reg5)=0 + func(reg3)=0 (write-mode 0, replace) |

Data: firefly target `[0x4F1C]`/`[0x4F1E]`; frame gate `[0x6BD5]`; firefly scratch
`[0x6BC0]`/`[0x6BC1]`; foreground flag table `[0x805E + tile]` (bit 0x40); tile-gfx
index `[0x8167 + tile*2]`; tilemap grid segment `word[0x2DDA]`.

**dos_re EGA gap (reusable):** `memory._ega_wb` does NOT emulate the Set/Reset
registers (GC index 0/1) — it writes CPU data to every map-masked plane. So the
firefly even/odd color-14/15 flicker collapses to color 15 under the VM oracle; any
future island that colors via Set/Reset will hit the same. Faithful matches the
oracle; `fireflies.firefly_color()` keeps the true flicker for the enhanced renderer.

## Tooling notes

- Disassembly truth: use **capstone on dumped bytes**, not the VM trace
  disassembler (it mis-computes some `Jcc` targets).
- Oracle capture: single-step the original to a routine's `RET`, not for a fixed
  instruction budget (post-routine code can overwrite output before you read it).
- A guessed invariant is not a verifier: a "decode length == header size" contract
  once falsely condemned a *correct* LZSS decoder (the header field is the output
  *reservation*, not the decode length). The authority is the lockstep vs ASM.
