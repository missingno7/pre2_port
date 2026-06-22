"""Byte-exact regression for the recovered screen-transition primitives.

`clear_span` (1030:32DE) — the horizontal span-clear used by the end-level scale/zoom
transition. Golden fixture captured from the original ASM under the VM (snapshot
002633, the tally/scale transition): for diverse spans that actually change pixels
(real partial-byte edge masks, not no-op out-of-bounds returns), the four EGA plane
bytes of the affected row before and after the ASM clear. The test runs the recovered
`clear_span` on the captured `before` planes and asserts it reproduces `after` exactly.

In-VM lockstep over real runs (002633 + 173821) confirmed 1073 spans, 0 divergence,
incl. 192 changed clears; this is the fast committed check.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.transition import clear_span, fade_palette

_FIX = Path(__file__).parent / "fixtures" / "transition" / "clear_span.json"
_FADE = Path(__file__).parent / "fixtures" / "transition" / "fade_palette.json"


def test_clear_span_byte_exact_vs_asm():
    data = json.loads(_FIX.read_text())
    W = data["window"]
    stride = data["stride"]
    cases = data["cases"]
    assert cases, "empty clear_span fixture"

    for it in cases:
        x, width, row, page = it["x"], it["width"], it["row"], it["page"]
        base = (row * stride + page) & 0xFFFF
        before = [bytes.fromhex(h) for h in it["before"]]
        after = [bytes.fromhex(h) for h in it["after"]]
        assert any(before[p] != after[p] for p in range(4)), "golden case must change pixels"

        planes = [bytearray(0x10000) for _ in range(4)]
        for p in range(4):
            planes[p][base:base + W] = before[p]

        clear_span(planes, x, width, row, page, stride)

        for p in range(4):
            got = bytes(planes[p][base:base + W])
            assert got == after[p], (
                f"x={x} width={width} row={row} plane{p}: span-clear mismatch\n"
                f"  got  {got.hex()}\n  want {after[p].hex()}"
            )


def test_fade_palette_byte_exact_vs_asm():
    """`fade_palette` (1030:6772) — golden fade steps captured from the ASM under the VM
    (snapshot 021225, a live palette fade). For each step the recovered fn must reproduce
    the 48 6-bit DAC components exactly and agree on the all-arrived (fade-done) flag.
    In-VM lockstep over the full fade confirmed 56 steps / 0 divergence + exact
    done-correspondence; this is the fast committed check."""
    data = json.loads(_FADE.read_text())
    cases = data["cases"]
    assert cases, "empty fade_palette fixture"
    for cse in cases:
        src = bytes.fromhex(cse["src"])
        target = bytes.fromhex(cse["target"])
        a, b = (target, src) if cse["direction"] != 0 else (src, target)
        out, arrived = fade_palette(a, b, cse["fade_amt"])
        assert out.hex() == cse["out"], (
            f"fade_amt={cse['fade_amt']}: DAC mismatch\n"
            f"  got  {out.hex()}\n  want {cse['out']}"
        )
        assert int(arrived) == cse["arrived"], f"fade_amt={cse['fade_amt']}: arrived flag"
