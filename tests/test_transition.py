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

import hashlib
import json
from pathlib import Path

import pre2.recovered.transition as transition
from pre2.recovered.transition import build_scaled_columns, clear_span, fade_palette

_DIR = Path(__file__).parent / "fixtures" / "transition"
_FIX = _DIR / "clear_span.json"
_FADE = _DIR / "fade_palette.json"
_SCOLS = _DIR / "scaled_columns.json"
_SFRAME = _DIR / "scale_frame.json"


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


def test_build_scaled_columns_byte_exact_vs_asm():
    """`build_scaled_columns` (1030:31F4) — the per-frame scaled-column geometry of the
    end-level scale transition. Golden: real ASM inputs (source tables + scale + offsets)
    captured under the VM (snapshot 002633); the recovered fn must reproduce the kept
    columns' [0x6B14]/[0x6A88] tables exactly. In-VM lockstep confirmed 40 frames / 0
    divergence."""
    data = json.loads(_SCOLS.read_text())
    assert data["cases"], "empty scaled_columns fixture"
    for cse in data["cases"]:
        xs, ys = build_scaled_columns(cse["src_x"], cse["src_y"], cse["scale"],
                                      cse["x_off"], cse["y_off"], cse["x_clamp"])
        assert [v & 0xFFFF for v in xs] == cse["xs"], "scaled X table mismatch"
        assert [v & 0xFFFF for v in ys] == cse["ys"], "scaled Y table mismatch"


def test_draw_scale_frame_geometry_vs_asm():
    """`draw_scale_frame` (1030:324B) — the border-clear pass. Golden: real captured
    inputs + the clear_span call sequence the recovered fn emits (independently verified
    byte-exact vs the ASM's VRAM, 15 frames / 0 divergence). This guards the geometry
    (which spans get cleared, in order); clear_span's pixel writes are tested separately."""
    data = json.loads(_SFRAME.read_text())
    assert data["cases"], "empty scale_frame fixture"
    real = transition.clear_span
    for cse in data["cases"]:
        calls: list[list[int]] = []
        transition.clear_span = lambda planes, x, width, row, pg, stride=0x28: \
            calls.append([x, width, row])
        try:
            transition.draw_scale_frame([None] * 4, cse["table_x"], cse["table_y"],
                                        cse["count"], cse["x_off"], cse["y_off"],
                                        cse["x_clamp"], cse["page"])
        finally:
            transition.clear_span = real
        assert len(calls) == cse["ncalls"], "clear_span call count"
        assert calls[:3] == cse["first3"] and calls[-3:] == cse["last3"]
        flat = [v for c in calls for v in c]
        h = hashlib.sha256(bytes(str(flat), "ascii")).hexdigest()[:16]
        assert h == cse["calls_sha16"], "clear_span call-sequence hash"
