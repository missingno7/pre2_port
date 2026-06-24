"""Byte-exact regression for the recovered mode-select MENU persistent page (1030:96D5 controller).

`pre2.recovered.menu_scene.MenuScenePage` owns the menu's evolving 4-plane page and applies the
already-recovered leaves (`draw_string` text stamps + `scroll_shift_frame` pans) the menu controller runs.

Fixture captured from the original ASM under the VM (driving snapshot_pre2_modeselect_20260623_075918
forward as the authoritative producer): the page state at a menu frame (seed), the font segment [0x2875],
and a sequence of per-frame leaf-call events (draw_string / scroll_shift inputs) with the SHA-256 of the
VM's four planes after each frame. Replaying the events through MenuScenePage must reproduce every hash.
Full-run lockstep over the menu (pre2/probes/verify_menu_scene.py + the mid-menu evolution drive) confirmed
diff=0 over 300 menu frames; this is the fast committed check.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

from pre2.recovered.menu_scene import PLANE_LEN, MenuScenePage

_FIX = Path(__file__).parent / "fixtures" / "menu"
_PAGE = 0x2000


def _seed_planes():
    raw = zlib.decompress((_FIX / "menu_seed.bin").read_bytes())
    return [raw[p * _PAGE:(p + 1) * _PAGE] for p in range(4)]


def test_menu_page_evolution_byte_exact_vs_asm():
    seed = _seed_planes()
    font = zlib.decompress((_FIX / "menu_font.bin").read_bytes())
    frames = json.loads((_FIX / "menu_frames.json").read_text())
    assert frames, "empty menu fixture"

    page = MenuScenePage()
    # Seed the owned page from the captured VM frame (the first 0x2000 of each plane; the leaves wrap at
    # 0x1FFF so the in-page evolution is closed over [:0x2000]).
    for p in range(4):
        page.planes[p] = bytearray(PLANE_LEN)
        page.planes[p][:_PAGE] = seed[p]
    page.seeded = True

    for fi, frame in enumerate(frames):
        for ev in frame["events"]:
            if ev[0] == "text":
                _, text, font_base, pen, advance, page_draw, page_clear = ev
                page.stamp_text(bytes(text), font, font_base, pen, advance, page_draw, page_clear)
            else:  # "shift"
                _, b199, sx, sy, psy, pd, bp = ev
                page.scroll_shift(b199, sx, sy, psy, pd, wrap=bp)
        got = hashlib.sha256(b"".join(bytes(page.planes[p][:_PAGE]) for p in range(4))).hexdigest()
        assert got == frame["hash"], f"menu page mismatch at frame {fi}: {got} != {frame['hash']}"


def test_menu_seed_lays_bg_planes():
    # The 9718 initial fill: planes 0,1 = the bg asset; planes 2,3 black.
    asset = bytes(range(256)) * 0x100        # 0x10000 synthetic asset
    page = MenuScenePage()
    page.seed(asset)
    assert page.seeded
    assert bytes(page.planes[0][:0x1F40]) == asset[:0x1F40]
    assert bytes(page.planes[1][:0x1F40]) == asset[0x1F40:0x3E80]
    assert not any(page.planes[2]) and not any(page.planes[3])
