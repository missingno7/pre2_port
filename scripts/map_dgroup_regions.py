"""DGROUP cartography — classify every byte of the game's data segment (the field-backed flip's map).

The field-backed migration (views -> real fields, the offset map into the detachable bridge) needs to know
what every DGROUP byte IS. This workbench tool builds that map from three independent evidence sources:

  1. THE VIEWS REGISTRY — every named field / StructArray element harvested from pre2.views.dgroup_view's
     descriptors (the single source of truth for name <-> offset <-> width).
  2. THE DIGEST MASKS — pre2.native.seams' forward-oracle exclusions (_FWD_EXCL): bytes the byte-exact
     gameplay proof already classifies as render/input-plumbing/audio-owned.
  3. INSTRUMENTED REPLAY — run recorded tick demos on the native core with a tracking byte image and record
     which DGROUP bytes gameplay actually READS and WRITES. (numpy buffer-protocol reads — the renderer —
     bypass the hooks; render state is mask-classified anyway.)

Every byte then lands in exactly one class:
    field / array          named in the views (the flip turns these into real fields)
    masked                 render/audio/input-owned per the digest mask (stays byte-shaped or is dropped)
    ACCESSED-UNNAMED       the GAP: gameplay touches it and nothing names it — the work list
    untouched              never accessed across the corpus (boot constants / tables the demos didn't reach
                           / dead) — cross-check against more demos before declaring dead

    python scripts/map_dgroup_regions.py [demo ...]      (default: the standing corpus's fast three)

Writes docs/pre2/dgroup_region_map.md (the summary + the compressed gap list, annotated with the nearest
named constants from the recovered modules).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

DGROUP_BASE = 0x1A0F << 4
DGROUP_END = DGROUP_BASE + 0x10000


# ---- 1. the views registry ----------------------------------------------------------------------------------

def harvest_views():
    """Every (offset, width, 'Class.field') the views name, absolute in DGROUP; arrays expanded per element."""
    import pre2.views.dgroup_view as dv

    named: dict[int, str] = {}

    def mark(off, width, label):
        for k in range(width):
            named.setdefault((off + k) & 0xFFFF, label)

    def field_descs(cls):
        for klass in cls.__mro__:
            for name, d in vars(klass).items():
                if isinstance(d, (dv._U8, dv._S8)):
                    yield name, d.off, 1
                elif isinstance(d, (dv._U16, dv._S16)):
                    yield name, d.off, 2

    for cls_name in dir(dv):
        cls = getattr(dv, cls_name)
        if not (isinstance(cls, type) and issubclass(cls, dv.StructView)):
            continue
        base = None
        if issubclass(cls, dv.DgroupView) and cls is not dv.DgroupView:
            base = 0
        if cls is dv.PlayerView:
            base = dv.PLAYER_BASE
        if base is None:
            continue                                    # bare record classes are covered via the arrays below
        for fname, off, width in field_descs(cls):
            mark(base + off, width, f"{cls_name}.{fname}")
        for aname, d in vars(cls).items():              # StructArray class attributes (incl. post-class ones)
            if isinstance(d, dv.StructArray):
                for i in range(d.length):
                    ebase = base + d.off + i * d.stride
                    for fname, off, width in field_descs(d.struct_cls):
                        mark(ebase + off, width, f"{cls_name}.{aname}[{i}].{fname}")
    # named mutable ARENAS (variable-stride record regions the flip keeps as owned byte-buffers)
    for label, (lo, hi) in getattr(dv, "ARENAS", {}).items():
        mark(lo, hi - lo + 1, f"ARENA {label}")
    return named


# ---- 2. the digest masks ------------------------------------------------------------------------------------

def harvest_masks():
    from pre2.native.seams import _FWD_EXCL
    return set(o for o in _FWD_EXCL if o < 0x10000)


# ---- 3. instrumented replay ---------------------------------------------------------------------------------

class TrackingBytearray(bytearray):
    """A byte image that records DGROUP reads/writes (int and slice access; buffer-protocol reads bypass)."""

    def __new__(cls, src):
        self = super().__new__(cls, src)
        self.reads: set[int] = set()
        self.writes: set[int] = set()
        return self

    def _mark(self, key, into):
        if isinstance(key, slice):
            start, stop, _ = key.indices(len(self))
            if stop > DGROUP_BASE and start < DGROUP_END:
                into.update(range(max(start, DGROUP_BASE) - DGROUP_BASE,
                                  min(stop, DGROUP_END) - DGROUP_BASE))
        else:
            if DGROUP_BASE <= key < DGROUP_END:
                into.add(key - DGROUP_BASE)

    def __getitem__(self, key):
        self._mark(key, self.reads)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self._mark(key, self.writes)
        super().__setitem__(key, value)


def replay_tracked(demo_dir: Path):
    """Run a recorded tick demo on the native core over a tracking image; return (reads, writes, n_ticks)."""
    from pre2.gaps import Pre2CaveTeleport, Pre2HybridGap, Pre2RespawnTransition
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.level_state import native_4f6c
    from pre2.native.loop import native_cave_teleport, native_gameplay_frame
    from pre2.native.state import NativeGameState

    gtd = GameTickDemo.load(demo_dir / "game_tick_demo.bin")
    img = TrackingBytearray(gtd.seed)
    state = NativeGameState(img)
    i = 0
    for i in range(gtd.n_ticks):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(state)
        except Pre2CaveTeleport as tp:
            for _ in native_cave_teleport(state, tp.si):
                pass
        except Pre2RespawnTransition:
            for _ in native_4f6c(state):
                pass
        except Pre2HybridGap:
            break                                       # level end / game over: the front end owns the rest
    return img.reads, img.writes, i + 1


# ---- annotations: the nearest named constants from the recovered/views modules --------------------------------

def harvest_constants():
    anchors: dict[int, str] = {}
    rx = re.compile(r"^([A-Z_][A-Z_0-9]*)\s*=\s*(0x[0-9A-Fa-f]+)")
    for pkg in ("pre2/recovered", "pre2/views", "pre2/native"):
        for f in (ROOT / pkg).glob("*.py"):
            for line in f.read_text(encoding="utf-8").splitlines():
                m = rx.match(line)
                if m:
                    v = int(m.group(2), 16)
                    if 0x20 <= v < 0x10000:
                        anchors.setdefault(v, f"{m.group(1)} ({f.stem})")
    return anchors


def compress(offsets):
    """Sorted offsets -> [(lo, hi)] ranges."""
    out = []
    for o in sorted(offsets):
        if out and o == out[-1][1] + 1:
            out[-1][1] = o
        else:
            out.append([o, o])
    return out


def main() -> int:
    demos = sys.argv[1:] or ["artifacts/demo_pre2_20260712_121135", "artifacts/demo_pre2_20260706_020106",
                             "artifacts/demo_pre2_full_gorilla_20260628_203423"]
    named = harvest_views()
    masked = harvest_masks()
    anchors = harvest_constants()

    reads, writes = set(), set()
    for d in demos:
        p = ROOT / d
        if not (p / "game_tick_demo.bin").exists():
            print(f"  (skipping {d}: no tick bin)")
            continue
        r, w, n = replay_tracked(p)
        print(f"  {p.name}: {n} ticks tracked, {len(r):,} bytes read, {len(w):,} written")
        reads |= r
        writes |= w

    accessed = reads | writes

    # THE FLIP RULE: mutable state must become fields; data gameplay never WRITES stays data (readable
    # through a TableView over the boot-constants blob). So read-only accessed bytes classify as data, and
    # the true gap = WRITTEN-and-unnamed. (Corpus caveat: rarely-written state can hide as read-only —
    # widen the demo set to firm the boundary.)
    classes = {}
    for o in range(0x10000):
        if o in named:
            c = "field/array"
        elif o in masked:
            c = "masked (render/audio/input)"
        elif o in writes:
            c = "WRITTEN-UNNAMED"
        elif o in reads:
            c = "read-only data (tables/defs)"
        else:
            c = "untouched"
        classes.setdefault(c, set()).add(o)

    gap = classes.get("WRITTEN-UNNAMED", set())
    gap_ranges = compress(gap)

    lines = ["# DGROUP region map — the field-backed flip's cartography",
             "",
             f"Generated by `scripts/map_dgroup_regions.py` over: {', '.join(Path(d).name for d in demos)}.",
             "Classes: **field/array** = named in the views; **masked** = the forward oracle's render/audio/",
             "input exclusion; **ACCESSED-UNNAMED** = gameplay touches it, nothing names it (the flip work",
             "list); **untouched** = never accessed across these demos (boot constants / unreached tables /",
             "candidate-dead — widen the corpus before declaring).",
             "",
             "| class | bytes | % |",
             "|---|---|---|"]
    for c in ("field/array", "masked (render/audio/input)", "read-only data (tables/defs)", "WRITTEN-UNNAMED", "untouched"):
        n = len(classes.get(c, ()))
        lines.append(f"| {c} | {n:,} | {n / 655.36:.1f}% |")
    lines += ["", f"## The gap — {len(gap):,} WRITTEN-unnamed bytes in {len(gap_ranges)} ranges", "",
              "| range | bytes | R/W | nearest anchor at/below |", "|---|---|---|---|"]
    for lo, hi in gap_ranges:
        n = hi - lo + 1
        rw = ("R" if any(o in reads for o in range(lo, hi + 1)) else "") + \
             ("W" if any(o in writes for o in range(lo, hi + 1)) else "")
        anchor = next((f"0x{a:04X} {anchors[a]}" for a in range(lo, max(lo - 0x400, -1), -1) if a in anchors),
                      "-")
        lines.append(f"| 0x{lo:04X}..0x{hi:04X} | {n} | {rw} | {anchor} |")
    out = ROOT / "docs" / "pre2" / "dgroup_region_map.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    print(f"\nwrote {out}")
    for c in ("field/array", "masked (render/audio/input)", "read-only data (tables/defs)", "WRITTEN-UNNAMED", "untouched"):
        print(f"  {c:32s} {len(classes.get(c, ())):6,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
