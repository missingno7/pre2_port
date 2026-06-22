"""Checkpoint for the music tracker / sequencer (1030:227C — one song tick).

Thin VM contact point only: it reads the playback state + the 4 voices + the current
pattern / order table / period table / instruments through the bridge
(``pre2.bridge.audio``), runs the recovered ``tracker_tick``, writes the updated
playback + per-voice state back, and returns. No sequencer logic lives here.

Live-hooked: in normal hybrid play the recovered tracker advances the song (the SB
hardware + the original mixer/ISR still run the rest). In verify mode the original
ASM tracker is the oracle and the recovered result is diffed against it at the
routine's RET (see pre2/probes/verify_tracker.py).
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import audio as _audio
from pre2.recovered.tracker import NUM_VOICES, tracker_tick

from .common import report

# GOG build: the per-tick sequencer is at 227C (volume-slide loop, then 22A1 tick
# countdown -> row advance / note-proc 2311); near RET at 230F.
_TICK_ENTRY = (0x1030, 0x227C)
_TICK_EXIT = (0x1030, 0x230F)


def _read_inputs(cpu):
    mem = cpu.mem
    pb = _audio.read_playback(mem)
    voices = [_audio.read_voice(mem, ch) for ch in range(NUM_VOICES)]
    pattern = _audio.read_current_pattern(mem, pb.order_pos)
    order = _audio.read_order_table(mem)
    song_len = _audio.read_song_length(mem)
    period_table = _audio.read_period_table(mem)
    instruments = _audio.read_tracker_instruments(mem)
    return pb, voices, pattern, order, song_len, period_table, instruments


def _run_recovered(pb, voices, pattern, order, song_len, period_table, instruments):
    tracker_tick(pb, voices, pattern, order, song_len, period_table, instruments)


def _write_back(mem, pb, voices) -> None:
    _audio.write_playback(mem, pb)
    for ch in range(NUM_VOICES):
        _audio.write_voice(mem, ch, voices[ch])


@registry.replace(*_TICK_ENTRY, "audio_tracker_tick")
def audio_tracker_tick(cpu) -> None:
    """Native replacement for the per-tick MOD sequencer at 1030:227C."""
    mem = cpu.mem
    pb, voices, *rest = _read_inputs(cpu)

    if getattr(cpu, "pre2_verify_mode", False):
        import copy
        snap = (copy.copy(pb), [copy.copy(v) for v in voices])
        _run_recovered(snap[0], snap[1], *rest)
        cpu.pre2_tracker_pending.append(snap)
        interpret_current_instruction_without_hook(cpu)
        return

    _run_recovered(pb, voices, *rest)
    _write_back(mem, pb, voices)
    cpu.s.ip = cpu.pop()  # near ret


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at 227C's RET."""
    from dataclasses import astuple

    def _verify_at_exit(c) -> None:
        if c.pre2_tracker_pending:
            pb_rec, voices_rec = c.pre2_tracker_pending.pop()
            mem = c.mem
            reason = None
            pb_asm = _audio.read_playback(mem)
            if astuple(pb_asm) != astuple(pb_rec):
                reason = f"playback asm={astuple(pb_asm)} rec={astuple(pb_rec)}"
            else:
                for ch in range(NUM_VOICES):
                    v_asm = _audio.read_voice(mem, ch)
                    if astuple(v_asm) != astuple(voices_rec[ch]):
                        reason = f"voice{ch} asm={astuple(v_asm)} rec={astuple(voices_rec[ch])}"
                        break
            report(stats, on_result, raise_on_divergence, "audio_tracker_tick", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_TICK_EXIT] = _verify_at_exit
    cpu.hook_names[_TICK_EXIT] = "audio_tracker_tick_verify"
