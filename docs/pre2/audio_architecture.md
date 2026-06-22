# PRE2 audio architecture

The game's audio is exposed as a stream of **semantic events** that two
interchangeable backends consume. The game says *what* it wants to hear; it no
longer dictates *how* the DOS mixer mixes it.

```
original VM / recovered game state
   -> recovered audio command layer        (pre2/bridge/audio_commands.py)
   -> semantic GameAudioEvent stream        (pre2/audio/events.py)
   -> FaithfulBackend | EnhancedBackend     (pre2/audio/{faithful,enhanced}_backend.py)
```

The recovered original mixer/tracker is **not** the final abstraction — it is the
archaeological reference and the verification oracle. The final abstraction is the
game-level audio command stream.

## 1. Recovered audio command layer  (`pre2/bridge/audio_commands.py`)

Knows the *layout* of the original command interface and emits semantic events; no
mixer internals leak past it. Command roots (GOG build, seg 1030):

| Command            | Entry      | Input / contract |
|--------------------|------------|------------------|
| **play SFX**       | `0x0282`   | `dl` = effect index. Digital path reads a 4-byte `{src,len}` descriptor at `DS:0x1009 + dl*4`, sample from segment `[0x0b59]`. (27 call sites.) |
| **start song**     | `0x02cc`   | parses the "M.K." ProTracker module from `[0x0b5e]` → `song_length` `[0xDC2]` + order table `[0xDC7]`. (8 call sites.) |
| **music enabled**  | `cs:[3]` bit `0x40` | music ON when the bit is *clear*. |

The non-digital SFX variant (`dl*10 + 0x1037`, PC-speaker/notes) is **unused on this
SB-digital build** (verified: 0 OPL/AdLib port writes during gameplay) and is reported
but not played.

Pure resolvers (`resolve_sfx`, `capture_module`) are unit-tested against the VM and
fixtures; `install_command_observers` adds transparent hooks that emit events while the
original audio path keeps running (so a backend can play alongside or instead of it).

## 2. Semantic events + assets  (`pre2/audio/events.py`, `assets.py`)

`GameAudioEvent`: `PlaySfx(sfx_id, pcm, volume, pan, priority)`, `StartSong(module,
loop, fade)`, `StopSong(fade)`, `SetMusicEnabled`, `SetSfxEnabled`, `SetVolume`. Events
are **self-contained** (they carry the resolved sample / `Module` asset) — a backend
needs neither VM memory nor the asset files. `Module`/`SampleAsset` are neutral: 8-bit
PCM + loop + the period→step table, no segment:offset / fill-buffer / DMA concepts.

## 3. FaithfulBackend  (`pre2/audio/faithful_backend.py`) — the oracle

Reproduces the **original byte-exact** output via the recovered tracker + mixer
(`AudioSystem`): 8-bit unsigned PCM, 168-byte blocks at the SB rate, 8-bit wrapping
add, channel-3-borrowed-by-SFX, the music flag. It deliberately keeps every original
constraint so its blocks stay identical to the ISR oracle.

* **Preserved for verification:** block size, 8-bit arithmetic, the volume table, the
  exact mix order. Fidelity proven by `pre2/probes/verify_audio_system.py` (in-VM
  lockstep, 40 blocks / 0 divergence).
* Depends on the recovered faithful internals on purpose.

## 4. EnhancedBackend  (`pre2/audio/enhanced_backend.py`) — modern ear-candy

Consumes the **same events** and renders **float32 / 44.1 kHz stereo**. Its music engine
is a clean **standard ProTracker player** (`pre2/audio/mod_player.py`) playing the
standard `.TRK` module carried by `StartSong` — standard Amiga period→frequency, standard
speed/BPM tempo, signed 8-bit samples, looping, the A/B/C/D/F effects PRE2 uses — with
per-voice linear-interpolated resampling, attack ramps (no edge clicks), and a soft
limiter. SFX are one-shot float voices from the resolved `SAMPLE.SQZ` bytes.

* **Roots, not internals.** It plays the song *asset* (`.TRK`), not the PRE2 mixer's
  compiled note-index/step form. The pressure test: `mod_player.py` + `enhanced_backend.py`
  import **only** numpy + the pure asset model — **no** `period_table`, resample step,
  8.4 kHz rate, 168-byte block, DMA/ISR, segment:offset, or the 8-bit volume table.
  (Enforced by `tests/test_audio_architecture.py::test_enhanced_path_has_no_vm_or_mixer_deps`.)
* The one original fact it honours is musical: the song's own speed/BPM tempo (from the
  module), which is *content*, not a mixer constraint.

This is the path you wire into the game; it is what `pre2_editor` does and what sounds
clean and click-free.

## Selecting a backend

`scripts/render_music.py <snapshot> --backend enhanced|faithful` renders either path to a
WAV. `enhanced` identifies the loaded song among the `.TRK` assets and plays it stereo
through `ModPlayer`; `faithful` runs the PRE2 oracle (8-bit, band-limit-resampled to
44.1 kHz mono).

## Fully detached: driven only by semantic events

The enhanced backend is liberated from the DOS audio machine. It is driven **only** by
semantic events from the recovered command layer and free-runs the song at its own musical
tempo; it never reads SB blocks, DMA, IRQ timing, or the original mixer's PCM.

* **Voice playback** — `ModPlayer.render_voices(n)` advances the active voices by `n`
  samples of audio time (continuous PCM); a held note sustains and is never gapped.
* **Sequencer** — `ModPlayer.tick()` advances the song one tick at its own tempo
  (`EnhancedBackend(free_run=True)`). The audio thread does both, paced solely by the
  audio device — so a slow/jittery video frame can never starve, gap, or speed the audio.

(`EnhancedBackend` also has an optional `advance_ticks` budget mode — sequencer gated by an
externally supplied game-audio tick — kept for experiments/tests, but the live and offline
paths both use `free_run`: pure detachment.)

## Live wiring  (`play.py --view --audio enhanced`)

`sdl_view.EnhancedAudio` owns the backend + a **dedicated audio thread**: SDL plays queued
PCM chunks on its own audio clock, and the thread keeps the channel fed by calling
`render(n)` — fully independent of the renderer/viewer loop.

The audio is driven entirely by the recovered command layer
(`pre2.bridge.audio_commands.install_command_observers(cpu, emit, assets_dir)`):

* hooks **play_sfx (0x0282)** at entry → emits `PlaySfx(resolve_sfx(dl))`;
* returns a **`poll(mem)`** the viewer calls each frame → on a song load (the
  `[0xDC2]`/`[0xDC7]` order signature changing) maps it to a `.TRK` via `identify_song` and
  emits `StartSong(module)`; also emits `SetMusicEnabled` on the `cs:[3]&0x40` change.

The original **ASM** runs the game's own audio (the recovered tracker/mixer checkpoints are
removed in this mode — they are the faithful/oracle path, and running the recovered mixer
live currently corrupts state via a known divergence). The ASM keeps the game's state
advancing; its SB PCM is **discarded, never played**. `EnhancedAudio.pump` only clears that
unused capture. Nothing of the old machine reaches the enhanced mixer.

## Notes

* **No OPL3/AdLib.** This GOG PRE2 detects the Sound Blaster and is digital-only;
  the vendored `nuked_opl3` FM backend and its viewer wiring were removed (0 OPL writes
  observed). `dos_re` keeps the generic `set_adlib_callback` capability (game-agnostic).
* Live viewer audio: `play.py --view --audio adlib` plays the faithful SB-DMA stream;
  `--audio enhanced` plays the modern backend driven by `install_command_observers`.
* **Faithful-from-standard gap (deepest root, deferred):** the faithful backend plays the
  PRE2 in-memory capture; making it consume `StartSong(.TRK)` byte-exact would require
  recovering the song loader's `.TRK`→in-memory conversion (0x02cc: standard Amiga period
  → note-index + the period→step table build). Not needed for the live (enhanced) game.
