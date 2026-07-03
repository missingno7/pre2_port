"""Map every boot-image byte the NATIVE runtime reads before writing (the true boot-constant set).

Wraps the boot image in a read-before-write tracking bytearray, then drives broad native coverage:
cold boot + gameplay ticks + render for EVERY level, plus the whole front-end flow (OLDIES -> titles ->
menu -> attract animation -> carte). The union of pristine reads, grouped into ranges and classified by
segment, is the data the boot constants must carry — everything else in the image (original code bytes,
DOS scaffolding) is dead weight the extraction drops.

    python -m pre2.probes.map_boot_reads
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

DS_BASE = 0x1A0F << 4
CS_BASE = 0x1030 << 4
DGROUP_END = DS_BASE + 0x10000


class TrackedData(bytearray):
    """bytearray recording which indexes are READ while still PRISTINE (never written since load)."""

    def init_track(self):
        n = len(self)
        self.written = np.zeros(n, dtype=bool)
        self.pristine_read = np.zeros(n, dtype=bool)
        self.track = True

    def __getitem__(self, i):
        if getattr(self, "track", False):
            if isinstance(i, slice):
                s = slice(*i.indices(len(self)))
                w = self.written[s]
                self.pristine_read[s] |= ~w
            else:
                if not self.written[i]:
                    self.pristine_read[i] = True
        return super().__getitem__(i)

    def __setitem__(self, i, v):
        if getattr(self, "track", False):
            if isinstance(i, slice):
                s = slice(*i.indices(len(self)))
                self.written[s] = True
            else:
                self.written[i] = True
        super().__setitem__(i, v)


def _ranges(mask: np.ndarray):
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    out = []
    s = p = int(idx[0])
    for o in idx[1:]:
        o = int(o)
        if o == p + 1:
            p = o
        else:
            out.append((s, p)); s = p = o
    out.append((s, p))
    return out


def main():
    import pre2.native.cold_boot as cb
    from pre2.native.boot_data import build_boot_memory
    from dos_re.dos import DOSMachine
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.render import native_load_level_palette, native_render, native_sync_render_state
    from pre2.native.state import NativeGameState
    from pre2.gaps import (Pre2CaveTeleport, Pre2GameOverTransition, Pre2HybridGap,
                           Pre2LevelEndTransition, Pre2RespawnTransition)

    gr = str(ROOT / "assets")
    raw = bytes(build_boot_memory())                     # the CONSTANTS-built image (boot_data.py)
    total = np.zeros(len(raw), dtype=bool)

    def tracked_boot():
        td = TrackedData(raw)
        td.init_track()
        return td

    # monkeypatch the memory builder so native_cold_boot builds over the tracked buffer
    orig_load = cb.build_boot_memory
    n_levels = len(list((ROOT / "assets").glob("LEVEL*.SQZ")))
    print(f"tracking {n_levels} levels x (cold boot + 40 ticks + render) ...")
    for lvl in range(n_levels):
        td = tracked_boot()
        cb.build_boot_memory = lambda: td
        try:
            state = cb.native_cold_boot(gr, level=lvl)
            dos = DOSMachine(gr)
            native_load_level_palette(state, dos)
            for _ in range(40):
                try:
                    native_gameplay_frame(state)
                except (Pre2CaveTeleport, Pre2RespawnTransition, Pre2LevelEndTransition,
                        Pre2GameOverTransition, Pre2HybridGap) as e:
                    print(f"  level {lvl}: stopped ticks on {type(e).__name__}")
                    break
            native_sync_render_state(state)
            disp = state.data[DS_BASE + 0x2DD6] | (state.data[DS_BASE + 0x2DD7] << 8)
            native_render(state, dos, disp, game_root=gr, force_gameplay=True)
        except Exception as e:                                    # noqa: BLE001 — coverage, record + continue
            print(f"  level {lvl}: {type(e).__name__}: {str(e)[:70]}")
        finally:
            cb.build_boot_memory = orig_load
        total |= td.pristine_read
        print(f"  level {lvl}: pristine reads so far {int(total.sum())}")

    # ---- the front-end flow (menu -> attract -> carte -> level), driven like test_native_attract_interrupt ----
    print("tracking the front-end flow ...")
    td = tracked_boot()
    cb.build_boot_memory = lambda: td
    try:
        from pre2.native.front_end import native_front_end
        from pre2.native.input import init_keyboard_input, set_key
        state = cb.native_cold_boot(gr, level=0)                  # loads over the same tracked buffer
        dos = DOSMachine(gr)
        init_keyboard_input(state)
        disp = state.data[DS_BASE + 0x2DD6] | (state.data[DS_BASE + 0x2DD7] << 8)
        gen = native_front_end(state, dos, disp, game_root=gr)
        d = state.data
        frames = 0
        for scene in gen:
            frames += 1
            if frames % 40 == 0:                                  # tap fire regularly to advance the scenes
                set_key(state, 0x39, True)
            elif frames % 40 == 20:
                set_key(state, 0x39, False)
            if frames == 300:                                     # let the menu idle -> attract fires
                d[DS_BASE + 0x27F0] = 0x0F; d[DS_BASE + 0x27F1] = 0x01
            if frames > 1500:
                break
        print(f"  front-end: {frames} frames")
    except Exception as e:                                        # noqa: BLE001
        print(f"  front-end: {type(e).__name__}: {str(e)[:80]}")
    finally:
        cb.build_boot_memory = orig_load
    total |= td.pristine_read

    # ---- classify + report ----
    def seg(a):
        if a < 0x10300: return "DOS/IVT"
        if a < CS_BASE + 0x9DF0: return "CS(code seg)" if a >= CS_BASE else "DOS/IVT"
        if DS_BASE <= a < DGROUP_END: return "DGROUP"
        if a >= 0x100000: return "VRAM planes"
        return "heap"

    ranges = _ranges(total)
    by = {}
    for a, b in ranges:
        by.setdefault(seg(a), []).append((a, b))
    print(f"\npristine-read total: {int(total.sum())} bytes in {len(ranges)} ranges")
    for k in ("DOS/IVT", "CS(code seg)", "DGROUP", "heap", "VRAM planes"):
        rs = by.get(k, [])
        n = sum(b - a + 1 for a, b in rs)
        print(f"\n== {k}: {n} bytes in {len(rs)} ranges ==")
        show = rs if k != "DGROUP" else rs[:8]
        for a, b in show[:40]:
            if k == "CS(code seg)":
                print(f"  cs:[{a - CS_BASE:04X}..{b - CS_BASE:04X}] ({b - a + 1}b)")
            elif k == "DGROUP":
                print(f"  ds:[{a - DS_BASE:04X}..{b - DS_BASE:04X}] ({b - a + 1}b)")
            else:
                print(f"  [{a:06X}..{b:06X}] ({b - a + 1}b)")
        if k == "DGROUP" and len(rs) > 8:
            print(f"  ... {len(rs) - 8} more DGROUP ranges (whole DGROUP ships anyway)")
    (ROOT / "artifacts" / "boot_read_map.json").write_text(json.dumps(ranges))
    print("\nwrote artifacts/boot_read_map.json")


if __name__ == "__main__":
    main()
