# PRE2 audio architecture

The game's audio is exposed as a stream of **semantic events** that two
interchangeable backends consume. The game says *what* it wants to hear; it no
longer dictates *how* the DOS mixer mixes it.

```
original VM / recovered game state
   -> recovered audio command layer        (pre2/bridge/audio_commands.py)
   -> semantic GameAudioEvent stream        (pre2/audio/events.py)
   -> live:   SdlEnhancedAudio              (scripts/sdl_view.py — SDL_mixer plays the .TRK)
      oracle: FaithfulBackend               (pre2/audio/faithful_backend.py — byte-exact)
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

## 4. Enhanced audio — the live, modern path

The live enhanced path is **`scripts/sdl_view.SdlEnhancedAudio`**: a command-driven player
that hands the whole identified `.TRK` module to **SDL_mixer's MOD player**
(`pygame.mixer.music.load(MOD)`), which streams it on SDL's own C audio thread.

* **It owns a continuous clock.** Music tempo is owned by the audio device, so it cannot be
  slowed by Python/VM/render/frame scheduling or any output-queue starvation. There is no
  Python render thread in the loop.
* **It plays the song asset, not the machine.** `StartSong(module)` →
  `mixer.music.load(.TRK as M.K. MOD) + play`; `StopSong` → stop; `SetMusicEnabled` →
  mute/unmute; `PlaySfx(sample)` → one-shot on a mixer Channel (resampled to the device rate).
  It never touches the recovered tracker/mixer, the Sound Blaster, DMA, IRQ, or original PCM
  — those stay on the faithful oracle path. This is what `pre2_editor`'s runtime does.

## Live wiring  (`play.py --view --audio enhanced`)

The game runs under the VM with a **detection-only Sound Blaster**
(`enable_sound_blaster(detection_only=True)`): it detects a digital device and runs its
song-loader / play-SFX command code, but no PCM streams and no playback IRQ fires (the
original audio ISR never runs). The audio is driven entirely by the recovered command layer
(`install_command_observers(cpu, emit, assets_dir)`):

* a hook on **play_sfx (0x0282)** at entry → emits `PlaySfx(resolve_sfx(dl))`;
* a per-frame **`poll(mem)`** → when a song loads (the `[0xDC2]`/`[0xDC7]` order signature
  changing identifies a `.TRK` via `identify_song`) emits `StartSong(module)`; also emits
  `SetMusicEnabled` on the `cs:[3]&0x40` change.

`SdlEnhancedAudio.post` consumes those commands and tells SDL_mixer what to play; SDL owns the
timing. Live diagnostics (songs / repeats / unidentified / sfx / errors) are surfaced on the
title-bar HUD by `pump()`.

## Offline oracle WAV

`scripts/render_music.py <snapshot>` renders the faithful oracle (the captured PRE2 module
through `FaithfulBackend`: 8-bit, resampled to 44.1 kHz) to a WAV — the offline debug/oracle
tool. The live enhanced sound is produced by SDL_mixer in the viewer, not here.

## Notes

* **No OPL3/AdLib.** This GOG PRE2 detects the Sound Blaster and is digital-only;
  the vendored `nuked_opl3` FM backend and its viewer wiring were removed (0 OPL writes
  observed). `dos_re` keeps the generic `set_adlib_callback` capability (game-agnostic).
* Live viewer audio: `play.py --view --audio adlib` plays the faithful SB-DMA stream;
  `--audio enhanced` is the SDL_mixer MOD player driven by `install_command_observers`.
* **Faithful-from-standard gap (deepest root, deferred):** the faithful backend plays the
  PRE2 in-memory capture; making it consume `StartSong(.TRK)` byte-exact would require
  recovering the song loader's `.TRK`→in-memory conversion (0x02cc: standard Amiga period
  → note-index + the period→step table build). Not needed for the live (enhanced) game.
