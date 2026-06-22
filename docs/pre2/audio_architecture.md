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

Consumes the **same events**, mixes in **float32 at 44.1 kHz**: per-voice linear-
interpolated resampling, short attack ramps (no edge clicks), a soft limiter. Free of
every DOS/SB constraint.

* **Must not leak in:** 8-bit wrapping arithmetic, the 168-byte DMA block, IRQ/ISR
  timing, segment:offset layout, the original 8.4 kHz output rate, the 8-bit volume
  table. (Enforced by `tests/test_audio_architecture.py::test_enhanced_backend_has_no_vm_or_mixer_deps`.)
* **Reused from the recovered layer:** only the pure *sequencer* (`recovered.tracker`)
  for musical note/effect decisions and tempo — that is song *content*, not mixer
  mechanics. The one original timing fact it honours is the song tick rate
  (`source_rate / 168` ≈ 50 Hz), which defines *tempo*, not output quality.
* Uses the original samples/modules purely as assets (8-bit PCM lifted to float).

## Selecting a backend

`scripts/render_music.py <snapshot> --backend enhanced|faithful` renders either path to
a WAV through the full pipeline (capture_module → StartSong → backend). The faithful
WAV is the 8-bit oracle band-limit-resampled to 44.1 kHz; the enhanced WAV is mixed in
float directly.

## Notes

* **No OPL3/AdLib.** This GOG PRE2 detects the Sound Blaster and is digital-only;
  the vendored `nuked_opl3` FM backend and its viewer wiring were removed (0 OPL writes
  observed). `dos_re` keeps the generic `set_adlib_callback` capability (game-agnostic).
* Live viewer audio (`play.py --view`) currently plays the faithful SB-DMA stream. A
  future step can drive a backend live from `install_command_observers` (needs a live
  audio-testing loop). See the memory note `pre2-audio-command-interface`.
