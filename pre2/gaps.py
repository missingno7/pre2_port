"""Shared scaffolding for the per-subsystem checkpoint adapters (+ the gap/transition exceptions).

Lives OUTSIDE ``pre2/checkpoints`` because the NATIVE (VM-less) layer raises/catches these too, and any
``pre2.checkpoints.*`` import executes the package __init__ — which eagerly imports every hook module (the
"import to register" hybrid surface) and thereby the whole VM. This module stays pure (no cpu/mem/dos_re
imports) so the standalone import closure stays VM-free.

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


class Pre2RespawnTransition(Pre2HybridGap):
    """Signal (not a gap): the per-frame gameplay step reached a multi-frame TRANSITION that must be driven
    outside the single-frame loop — the death-respawn (4C69's [0x6be4]==1 -> 4F6C). The death-bounce plays out
    60 rendered frames; running it blocking inside ``native_gameplay_frame`` would make the runner render only
    the end state (instant respawn, no animation). So ``native_level_state`` raises this and the runtime/flow
    driver drives ``native_4f6c`` as a generator, rendering each frame. Subclasses Pre2HybridGap so existing
    ``except Pre2HybridGap`` sites still treat it as "not a plain per-frame step" — catch it FIRST where the
    transition is actually driven."""


class Pre2LevelEndTransition(Pre2HybridGap):
    """Signal (not a gap): the per-frame gameplay step reached the LEVEL-END transition (4C69's [0x6be6]==1 ->
    4cba -> 4F65). The level ends, the next level loads, and gameplay continues there — a level change driven
    outside the single-frame loop (like the respawn). ``native_level_state`` raises this; the runtime/flow driver
    drives ``native_level_end`` (increment the level, load + re-init the next level, set the level-change flags).
    Subclasses Pre2HybridGap so existing ``except Pre2HybridGap`` sites still treat it as "not a plain per-frame
    step" — catch it FIRST where the transition is actually driven."""


class Pre2GameOverTransition(Pre2HybridGap):
    """Signal (not a gap): the per-frame gameplay step reached the DEATH -> GAME-OVER-RESTART transition (4C69's
    [0x6be5]==1 -> 5063). The death-bounce plays out (509d, 60 rendered frames), then the game resets to level 1
    with a zero score and re-enters main at 0x12f to reload level 1 — a game restart driven outside the
    single-frame loop. ``native_level_state`` raises this; the runtime/flow driver drives ``native_5063`` as a
    generator (rendering the bounce) then reloads level 1 (native_level_init) and the gameplay loop continues.
    Subclasses Pre2HybridGap so existing ``except Pre2HybridGap`` sites still treat it as "not a plain per-frame
    step" — catch it FIRST where the transition is actually driven."""


class Pre2GameComplete(Pre2HybridGap):
    """Signal (not a gap): the per-frame gameplay step reached GAME-COMPLETE — the player fell out of the final
    level 0xE ([0x2d8a]==0xE), so the fall handler armed [0x6be5]=0xFF ([asm 5B18-5B1F]) and 4C69 dispatches to
    the ending routine 5034: load THEEND.SQZ, deplanarize/fade it in (919F), wait for fire (0BBE), fade out
    (9286), then carry -> main's 0x12f which returns to the front-end (attract/title). ``native_level_state``
    raises this; the runtime/flow driver drives ``native_the_end`` (the scene) then re-enters the front-end.
    Subclasses Pre2HybridGap so existing ``except Pre2HybridGap`` sites still treat it as "not a plain per-frame
    step" — catch it FIRST where the transition is actually driven."""


class Pre2CheatCredits(Pre2HybridGap):
    """Signal (not a gap): the 247B cheat-combo fired mid-gameplay — Left Ctrl + Left Alt + scancode 0x11 (W on
    QWERTY / Z on the French AZERTY the game was built on, for Eric ZMIRO) held with NO other key down. It shows
    the hidden DEVELOPER-CREDITS screen (2505: the same 0Dh OLDIES font/palette, the dev-name line script), waits
    for fire (0BBE), restores the level palette (0BA0), and returns to gameplay where it left off. A pure overlay
    (no gameplay-state change), so ``native_player_step`` raises it and the flow driver shows the scene then resumes
    the SAME level. Subclasses Pre2HybridGap; catch it FIRST where the credits scene is driven."""


class Pre2CaveTeleport(Pre2HybridGap):
    """Signal (not a gap): the position-trigger scan (52FE) matched a cave/teleport entrance — the multi-frame
    5326 transition (vertical fade-out curtain 30C6, the hidden camera pan to the destination, the 53D7
    mini-pass, the 3054 center-out reveal, then the frame's remainder). ``native_trigger_scan`` raises this
    BEFORE mutating anything; the runtime/flow driver drives ``native_cave_teleport(state, si)`` as a generator,
    rendering each yielded phase (a state-only consumer just drains it). ``si`` = the matched [0x8367] table
    entry offset. Subclasses Pre2HybridGap so existing ``except Pre2HybridGap`` sites still treat it as "not a
    plain per-frame step" — catch it FIRST where the transition is actually driven."""

    def __init__(self, si: int):
        super().__init__(f"cave teleport (5326) at table entry {si:#06x}")
        self.si = si


@dataclass
class HookVerifyStats:
    verified: int = 0
    diverged: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class HookTraceStats:
    """Per-hook invocation counts for the live hybrid runtime — which recovered systems
    are actually firing (and, by their absence, which screens are still pure ASM). No
    oracle, no diff: just a tally of the real replacement hooks as they run."""
    counts: dict = field(default_factory=dict)

    def bump(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def total(self) -> int:
        return sum(self.counts.values())

    def snapshot(self) -> dict:
        """A copy of the cumulative counts — pass to ``summary``/``window_total`` as ``since``
        to get a *window* (delta) view: only the hooks that fired since that snapshot."""
        return dict(self.counts)

    def window_total(self, since: dict | None) -> int:
        """Total fires since the ``since`` snapshot (cumulative total if ``since`` is None)."""
        if since is None:
            return self.total()
        return sum(max(0, v - since.get(k, 0)) for k, v in self.counts.items())

    def summary(self, group=None, top: int | None = None, since: dict | None = None) -> str:
        """One-line ``name=count`` summary. With ``since`` (a prior :meth:`snapshot`) show only
        the DELTA — the hooks firing in this window — instead of the cumulative totals."""
        src = self.counts
        if since is not None:
            src = {k: v - since.get(k, 0) for k, v in self.counts.items() if v - since.get(k, 0) > 0}
        agg: dict[str, int] = {}
        for name, c in src.items():
            g = group(name) if group else name
            agg[g] = agg.get(g, 0) + c
        items = sorted(agg.items(), key=lambda kv: -kv[1])
        if top is not None:
            items = items[:top]
        empty = "(idle)" if since is not None else "(no recovered hooks fired)"
        return " ".join(f"{n}={c}" for n, c in items) or empty


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
