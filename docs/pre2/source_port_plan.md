# Prehistorik 2 source-port plan

## Non-negotiable boundary

`dos_re` is the reusable DOS machine.  It must not know Prehistorik 2 addresses, assets, command-line quirks, or source-port assumptions.

`pre2` is the game-specific layer.  It owns original PRE2 filenames, executable inventory, bootstrap helpers, future address maps, and later verified hooks.

## Phase status

**Bootstrap — done.** Boot the packed `pre2.exe` through the VM; treat LZEXE as bootstrap
(target-neutral accelerator); collect snapshots; trace `.sqz`/`.trk` loads; render the screens.

**Recovery — the native VM-less game plays.** `scripts/play_native.py` cold-boots the whole game with
**no emulator** (OLDIES → titles → menu → carte → all levels → tally → endings → game-over/restart),
from boot constants (`pre2/native/boot_data.py`, no EXE) + the GOG assets. The entire per-frame loop is
recovered source — rendering, the gameplay update (player FSM / movement / collision, the object &
second passes, terrain/effects, the `88D7` combat pass, the `4C69` level state machine), digital SFX,
and per-level music. The `dos_re` VM is kept **only as an offline oracle**: a recorded demo is replayed
through the ASM and the native core tick-by-tick and the DGROUP compared (byte-exact). `scripts/deploy_native.py`
ships a standalone folder + PyInstaller exe with no VM/EXE. The **hybrid** runtime (`play.py`) remains the
workbench where a new island is prepared and proven before the same recovered code runs native.

Each island: find the ASM/data boundary, define the input/output contract, observe I/O, write clean native
logic, verify byte-exact against the ASM, wire the thin adapter — then lift it into the native core.

### Recovered islands

Each island declares the larger subsystem it will **merge into** (the coastline must move upward
over time — see `recovery_architecture.md`; hooks are scaffolding, not the final architecture).

The authoritative island list is **generated from the code** — each recovered function carries its own
`@oracle_link(boundary, contract, status, merge_target)` metadata (`pre2/islands.py`), auto-discovered into
[`recovered_islands.md`](recovered_islands.md) (regenerate: `python scripts/gen_island_manifest.py`; a test
fails on drift). **That manifest — ~100+ islands — is the source of truth for what is recovered.** As of this
writing the recovered set spans the whole runtime: the asset codecs; the full render pipeline (sprite decode /
classify / blit, the object-list draw `26FA`, the frame renderer, HUD, iris, particles/fireflies/foreground,
parallax, LEVELG snow); the whole gameplay update (player FSM + movement + collision, the object and second
passes, terrain/moving-platform entities, the effects passes, the `88D7` combat pass, the `4C69` level state
machine); digital SFX + per-level music; and the front-end flow (intro/titles/attract/menu/password/carte).

**Coastline residuals (small, fail-loud, do not block a normal playthrough):**
- a few rare edge-case gameplay paths still fail loud rather than run (e.g. the game-over-via-respawn tail
  `506c`, a vertically/horizontally blocked cave-pan);
- the level-end tally shows the exact score/percent but not yet the animated count-up *cutscene* (`4CCB`);
- some deferred loader/render decor (parallax detail `0x9dc0`, trigger-sprite/self-patch visuals);
- SoundBlaster/DMA/PIC emulation (`dos_re`) is the audio **oracle**; a fully recovered `AudioSystem` (below)
  is the remaining audio lift.

## Audio recovery (layered)

The emulated SoundBlaster/DMA/PIC (`dos_re`) + the original ASM audio driver are the **oracle/bootstrap
path, not the final architecture**. The goal is a clean recovered **`AudioSystem`** so hybrid play needs
neither the ASM driver nor the emulated SB (which stay as oracle/verify backends). Recover in layers, with
verification rising from bytes → state → PCM:

1. **Asset decode** — **DONE** (`pre2/codecs/audio.py`, `tests/test_audio_assets.py`): `.TRK` = SQZ-LZSS
   standard ProTracker **M.K.** module (all 12 parse, layout closes exactly); `SAMPLE.SQZ` = SQZ-"other" →
   60768-byte 8-bit PCM SFX bank. (SQZ decode itself was already recovered.)
2. **Data model** — `SampleBank`/`Module`/`Pattern`/`Instrument`/`ChannelState`/… (`ModModule`/`ModSample`
   exist); raw layout → `pre2/bridge/audio.py`.
3. **Tracker/playback** — sequencer `1030:227C` → `pre2/recovered/tracker.py` (only effects PRE2 uses).
4. **Mixer** — per-channel `1030:218F` + SFX `20AB-20F3` + DMA-refill ISR `20AB` → `pre2/recovered/mixer.py`;
   verify same state+SFX+timing → same PCM block vs `sb.pcm_out`.
5. **Integration** — detach hybrid play from the ASM audio path (recovered `AudioSystem.tick → mixer →
   backend`); ASM/SB stay oracle-only.

`dos_re` holds **no** PRE2-specific audio knowledge; tracker/mixer logic never lives in checkpoints/adapters.
"Audio works via emulated SB + original driver" ≠ "audio decode/mixer recovered."

## Recovery rules (kept short; full posture in `recovery_architecture.md`)

- Three explicit modes; the original ASM runs only in **oracle**/**verify** modes,
  never as a silent fallback. Hybrid mode fails loud on gaps (`Pre2HybridGap`).
- Recovered logic is clean, VM-independent (no `cpu`/`mem`/`dos_re`); hooks are
  thin adapters/verifiers with a declared role (probe / verifier / replacement /
  gap-detector), not where logic accumulates.
- Dataclasses reconstruct the original C-like structs; the bridge layer reads them
  from VM memory and (when replacing) writes them back. Verification rises from
  byte/buffer diffs to semantic state contracts over time.
- **Gameplay logic speaks human-named fields, not raw offsets.** The DGROUP layout is
  quarantined in one view layer (`pre2/bridge/dgroup_view.py`) with swappable backends
  (byte / overlay / write-contract); the recovered logic is agnostic to which is behind it.
  The byte-backed representation is a legitimate release citizen — it is not the EXE, not a
  VM, and not a fallback — and keeps behaviour + verification the *same bytes*. Full design +
  scope in [`state_view_layer.md`](state_view_layer.md).

## Reference

- Original addresses, continuation points, allocator state, and decode boundaries:
  [`symbol_ledger.md`](symbol_ledger.md).
- `pre2.exe` is LZEXE 0.91-packed MZ; the asset set is dozens of `.sqz` (recovered
  decompressor) and `.trk` music files.
