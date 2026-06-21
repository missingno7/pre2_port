"""Shared scaffolding for the per-subsystem checkpoint adapters.

A *checkpoint* is a thin contact point between the original PRE2 ASM and a
recovered, VM-independent module — a replacement adapter (hybrid runtime) and/or
a lockstep verifier (oracle diff). It is **scaffolding, not architecture**: the
recovered logic lives in ``pre2/recovered`` + ``pre2/codecs`` and the data model in
``pre2/bridge``; everything here just bridges register/memory state to those.
See docs/pre2/source_port_plan.md (the "coastline" posture).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# GOG data segment + load pointer (used by sprite_decode; sqz/blit carry their own).
_DATA_SEG = 0x1A0F
_BUMP_PTR = 0x2875


def _read_cstring(mem, seg: int, off: int) -> str:
    base = ((seg << 4) + off) & 0xFFFFF
    end = mem.data.find(0, base, base + 128)
    if end < 0:
        end = base + 128
    return mem.data[base:end].decode("latin1")


class Pre2HybridGap(RuntimeError):
    """The hybrid runtime reached something not yet recovered.

    Raised loudly instead of silently falling back to the original ASM — a silent
    fallback would hide missing recovery work (see the "fail-fast over guessed
    fallback" rule in docs/dos_re/source_port_methodology.md).
    """


@dataclass
class HookVerifyStats:
    verified: int = 0
    diverged: list[tuple[str, str]] = field(default_factory=list)


def report(stats: HookVerifyStats, on_result, raise_on_divergence, name: str, reason):
    """Record one verify outcome: ``reason is None`` means the contract matched.

    Centralises the verified/diverged bookkeeping every subsystem verifier shares,
    so each checkpoint module only computes its own contract diff.
    """
    if reason is None:
        stats.verified += 1
        if on_result is not None:
            on_result(name, True, None)
    else:
        stats.diverged.append((name, reason))
        if on_result is not None:
            on_result(name, False, reason)
        if raise_on_divergence:
            raise AssertionError(f"hook verify divergence on {name}: {reason}")
