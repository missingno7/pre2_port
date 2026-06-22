"""Checkpoint for the software audio mixer (1030:218F per-channel mix).

Thin VM contact point only: it reads the channel/instrument/volume state through the
bridge (``pre2.bridge.audio``), runs the recovered ``mix_channel`` on the live DMA
block, writes the updated channel state back, and returns. No mixer logic lives here.

Live-hooked: in normal hybrid play the recovered mixer produces the per-channel mix
(the SB hardware + the original driver's ISR/SFX/tracker still run the rest). In
verify mode the original ASM mixer is the oracle and the recovered result is diffed
against it at the routine's RET. The full software mixer is verified byte-exact (see
pre2/probes/verify_mixer.py / verify_mixer_block.py).
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import audio as _audio
from pre2.recovered.mixer import BLOCK_LEN, CHANNEL_OFF, mix_channel

from .common import report

# GOG build: the per-channel mixer is at 218F (entry) / 227B (near RET).
_MIX_ENTRY = (0x1030, 0x218F)
_MIX_EXIT = (0x1030, 0x227B)


def _read_inputs(cpu):
    mem = cpu.mem
    ch = (cpu.s.bx & 0xFFFF) >> 1
    cs = _audio.read_channel(mem, ch)
    return ch, cs


@registry.replace(*_MIX_ENTRY, "audio_mix_channel")
def audio_mix_channel(cpu) -> None:
    """Native replacement for the per-channel MOD mixer at 1030:218F."""
    mem = cpu.mem
    ch, cs = _read_inputs(cpu)

    if getattr(cpu, "pre2_verify_mode", False):
        buf_flat = _audio.fill_buffer_flat(mem)
        snap = bytearray(mem.data[buf_flat:buf_flat + BLOCK_LEN])
        if cs.pos != CHANNEL_OFF:
            instr = _audio.read_instrument(mem, cs.instrument, cs.end)
            new_cs = mix_channel(snap, cs, instr, _audio.read_volume_table(mem))
        else:
            new_cs = cs
        cpu.pre2_audio_pending.append((ch, buf_flat, snap, new_cs))
        interpret_current_instruction_without_hook(cpu)
        return

    if cs.pos != CHANNEL_OFF:                       # off channel: 218F writes nothing
        instr = _audio.read_instrument(mem, cs.instrument, cs.end)
        buf_flat = _audio.fill_buffer_flat(mem)
        buf = bytearray(mem.data[buf_flat:buf_flat + BLOCK_LEN])
        new_cs = mix_channel(buf, cs, instr, _audio.read_volume_table(mem))
        mem.data[buf_flat:buf_flat + BLOCK_LEN] = buf
        _audio.write_channel(mem, ch, new_cs)
    cpu.s.ip = cpu.pop()  # near ret (218F clobbers regs the caller reloads; sp is its own scratch)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at 218F's RET."""

    def _verify_at_exit(c) -> None:
        if c.pre2_audio_pending:
            ch, buf_flat, snap, new_cs = c.pre2_audio_pending.pop()
            mem = c.mem
            reason = None
            asm_block = bytes(mem.data[buf_flat:buf_flat + BLOCK_LEN])
            if asm_block != bytes(snap):
                i = next(k for k in range(BLOCK_LEN) if asm_block[k] != snap[k])
                reason = f"ch{ch} pcm@{i}: asm={asm_block[i]:02X} rec={snap[i]:02X}"
            else:
                a = _audio.read_channel(mem, ch)
                if (a.pos, a.end, a.frac) != (new_cs.pos, new_cs.end, new_cs.frac):
                    reason = (f"ch{ch} state asm=(pos={a.pos:04X} end={a.end:04X} frac={a.frac:02X}) "
                              f"rec=(pos={new_cs.pos:04X} end={new_cs.end:04X} frac={new_cs.frac:02X})")
            report(stats, on_result, raise_on_divergence, "audio_mix_channel", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_MIX_EXIT] = _verify_at_exit
    cpu.hook_names[_MIX_EXIT] = "audio_mix_channel_verify"
