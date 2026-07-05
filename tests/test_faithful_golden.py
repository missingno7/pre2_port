"""The FAITHFUL-FRAME GOLDEN — the render regression net for the enhancement era.

Pins the faithful native render (``native_render`` planes + page + palette) on four deterministic
cold-boot states as committed sha256 goldens. The faithful frame is the byte-exact-derived baseline every
enhancement layers on top of; from here on, render-path refactors (the FrameSnapshot capture seam, the
compositor port, interpolation) can be made aggressively — a golden mismatch means the FAITHFUL output
changed, which must never happen as a side effect.

Determinism: ``native_cold_boot`` is a pure function of the boot constants + the game assets (the RNG seed
is the recovered BIOS-checksum constant), ``native_gameplay_frame`` with no injected input is pure state →
state, and the render is a pure function of the state — proven by generating each hash twice at pin time.

The four states cover the render feature matrix: L1 (baseline gameplay), level id 4 (vertical scroll),
0x0D (the earthquake proximity-scenery level), 0x0F (LEVELG — the falling snow, whose render CONSUMES the
gameplay rng, the one render/state-coupled effect).

If a golden mismatches after an INTENTIONAL faithful-render change (a recovered improvement proven against
the VM), regenerate with the printed hash — but that must be a deliberate, verified decision, never drive-by.
"""
from __future__ import annotations

import hashlib
import pathlib

import pytest

ASSETS = pathlib.Path(__file__).resolve().parent.parent / "assets"

# (level id, ticks) -> sha256 of planes + page + 16-colour palette, pinned 2026-07-05 (both runs identical).
GOLDENS = {
    (0x00, 120): "edf39ab1c85782ce0e453b771ac4b54f23f1615b8bbddcbc04a38d7271b799cb",
    (0x04, 150): "75d95b5ee82aab1413110e8025d2033d172ba6870d4246467be593d6b0c46e04",
    (0x0D, 120): "c439538e79292a9538b8ab689fd795ca86a9a40af274de1d852da8319a8e5671",
    (0x0F, 120): "ec097f3358fff6cd87a1b7688ec533e4c9ae53313980c91a0677631b069daed4",
}


def _faithful_hash(level: int, ticks: int) -> str:
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.render import (native_load_level_palette, native_render,
                                    native_sync_render_state)
    from pre2.native.vga import NativeVGA

    state = native_cold_boot(str(ASSETS), level=level)
    dos = NativeVGA()
    native_load_level_palette(state, dos)
    for _ in range(ticks):
        native_gameplay_frame(state)
    native_sync_render_state(state)
    ds = 0x1A0F << 4
    disp = state.data[ds + 0x2DD6] | (state.data[ds + 0x2DD7] << 8)
    planes, page = native_render(state, dos, disp, game_root=str(ASSETS), force_gameplay=True)
    h = hashlib.sha256()
    for p in planes:
        h.update(bytes(p))
    h.update(page.to_bytes(2, "little"))
    for c in dos.vga_palette[:16]:
        h.update(bytes(c))
    return h.hexdigest()


@pytest.mark.skipif(not (ASSETS / "pre2.exe").exists(), reason="game assets not present")
@pytest.mark.parametrize("level,ticks", sorted(GOLDENS))
def test_faithful_frame_golden(level: int, ticks: int) -> None:
    got = _faithful_hash(level, ticks)
    assert got == GOLDENS[(level, ticks)], (
        f"FAITHFUL render changed on level {level:#04x} @ {ticks} ticks: {got}\n"
        f"The faithful frame is the byte-exact baseline — an enhancement/refactor must never alter it. "
        f"Only re-pin after an intentional, VM-verified faithful-render change."
    )
