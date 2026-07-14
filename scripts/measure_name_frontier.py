"""Measure the NAME frontier: of every DGROUP access the gameplay tick makes, how many already go through a
NAME-capable view (dgroup_view descriptors, which resolve via backend.read_field when present) versus how many
are RAW pointer arithmetic (recovered/native code calling rb/rw/wb/ww directly with a computed offset — no
name, so un-resolvable by a pure offset-free backend).

The raw set is the exact remaining "dissolve" work for a whole-tick offset-free run: each raw site must become
a named record/array access (like the object_tick / combat / player_interaction grinds already did for the
pools they cover). This quantifies what's left and ranks it by call site.

Method: run the tick on the bridge DataclassBackend (byte-exact, offset-based) wrapped so every rb/rw/wb/ww is
classified by its immediate caller — a dgroup_view.py descriptor (VIEW, name-resolvable) or anything else (RAW,
walked up past the state.py / memory_adapter.py accessor plumbing to the real call site).

    python scripts/measure_name_frontier.py [max_ticks]
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from pre2.bridge.game_layout import DataclassBackend
from pre2.gaps import Pre2HybridGap
from pre2.native.game_tick_demo import GameTickDemo, _inject
from pre2.native.loop import native_gameplay_frame
from pre2.native.state import NativeGameState

_PLUMBING = ("native/state.py", "views/memory_adapter.py", "measure_name_frontier.py")

# Not every RAW access is a gameplay-logic dissolve target. Categorise the raw modules so the TRUE remaining
# logic frontier is honest (a bulk region copy or a read-only table read is not "offset arithmetic to name").
_CATEGORY = {
    "firefly_sim.py": "bulk",       # the 160-byte swarm-slot blob is serialised as a bytearray, not per-field
    "object_render.py": "render",   # a render concern — runs over the materialised image by design
    "particles.py": "render",       # the render-snapshot reader (views/particles.py)
    "tables.py": "loaded",          # read-only loaded lookup tables (props/bytecode) — loaded input, not state
    "game_tick_demo.py": "harness",  # _inject — the demo harness, not the shipped tick
}
_NOT_FRONTIER = {"bulk", "render", "loaded", "harness"}


class _Instrumented:
    """Wraps a DataclassBackend, classifying every access, delegating the actual read/write unchanged."""

    _IS_DGROUP_BACKEND = True

    def __init__(self, inner):
        self._inner = inner
        self.view = Counter()
        self.raw_sites = Counter()
        self.raw_offs = {}

    def _hit(self, off):
        # 0=_hit, 1=wrapper rb/rw/wb/ww, 2=immediate caller. Walk up past the accessor plumbing
        # (NativeGameState.rb in state.py, readers() in memory_adapter.py, this wrapper) to the REAL site.
        f = sys._getframe(2)
        while f is not None and any(f.f_code.co_filename.replace("\\", "/").endswith(p) for p in _PLUMBING):
            f = f.f_back
        if f is None:
            self.raw_sites["<unknown>"] += 1
            return
        # a dgroup_view descriptor (_read_field/_write_field/array/slot views) = a name-CAPABLE view access
        if f.f_code.co_filename.replace("\\", "/").endswith("views/dgroup_view.py"):
            self.view["view"] += 1
            return
        site = f"{Path(f.f_code.co_filename).name}:{f.f_code.co_name}"
        self.raw_sites[site] += 1
        self.raw_offs.setdefault(site, set()).add(off & 0xFFFF)

    def rb(self, off):
        self._hit(off); return self._inner.rb(off)

    def rw(self, off):
        self._hit(off); return self._inner.rw(off)

    def wb(self, off, v):
        self._hit(off); return self._inner.wb(off, v)

    def ww(self, off, v):
        self._hit(off); return self._inner.ww(off, v)

    def materialize(self, *a):
        return self._inner.materialize(*a)


def main() -> int:
    max_ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    demos = ["demo_pre2_20260704_235611", "demo_pre2_full_gorilla_20260628_203423",
             "demo_pre2_finish_game_norepl_20260703_165400"]
    view_total = 0
    raw_total = 0
    raw_sites = Counter()
    raw_offs: dict = {}
    ticks = 0
    for d in demos:
        p = ROOT / "artifacts" / d / "game_tick_demo.bin"
        if not p.exists():
            continue
        gtd = GameTickDemo.load(p)
        st = NativeGameState(bytearray(gtd.seed))
        inst = _Instrumented(DataclassBackend(st, readonly_image=False))
        st.backend = inst
        for i in range(min(gtd.n_ticks, max_ticks)):
            _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            try:
                native_gameplay_frame(st)
            except Pre2HybridGap:
                break
            ticks += 1
        view_total += sum(inst.view.values())
        raw_total += sum(inst.raw_sites.values())
        raw_sites.update(inst.raw_sites)
        for s, offs in inst.raw_offs.items():
            raw_offs.setdefault(s, set()).update(offs)

    total = view_total + raw_total
    print(f"=== NAME frontier over {ticks} ticks ({total:,} DGROUP accesses) ===")
    print(f"  VIEW-routed (name-capable): {view_total:,}  ({100*view_total/total:.1f}%)")
    print(f"  RAW pointer arithmetic:     {raw_total:,}  ({100*raw_total/total:.1f}%)")

    by_module: Counter = Counter()
    mod_offs: dict = {}
    for site, n in raw_sites.items():
        mod = site.split(":", 1)[0]
        by_module[mod] += n
        mod_offs.setdefault(mod, set()).update(raw_offs.get(site, ()))

    # split raw into the TRUE gameplay-logic frontier vs the not-a-target categories
    cat_totals: Counter = Counter()
    logic_raw = 0
    for mod, n in by_module.items():
        cat = _CATEGORY.get(mod, "logic")
        cat_totals[cat] += n
        if cat == "logic":
            logic_raw += n
    frontier_denom = view_total + logic_raw
    print("\n  RAW categorised (only 'logic' is a dissolve target):")
    for cat, n in cat_totals.most_common():
        print(f"    {n:>9,}  {cat}")
    print(f"\n  >>> TRUE gameplay-logic frontier: {view_total:,} named / {frontier_denom:,} nameable"
          f"  = {100*view_total/frontier_denom:.1f}% done  ({logic_raw:,} logic-raw left)")

    print(f"\n  RAW-LOGIC by module ({sum(1 for m in by_module if _CATEGORY.get(m,'logic')=='logic')}), "
          f"ranked — the real dissolve roadmap:")
    for mod, n in by_module.most_common():
        if _CATEGORY.get(mod, "logic") == "logic":
            print(f"    {n:>9,}  {mod:<28} ({len(mod_offs.get(mod, ())):>3} offsets)")
    print("\n  RAW not-a-target (excluded from the frontier):")
    for mod, n in by_module.most_common():
        if _CATEGORY.get(mod, "logic") != "logic":
            print(f"    {n:>9,}  {mod:<28} [{_CATEGORY[mod]}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
