"""Phase 5 proof-in-progress: the WHOLE product path (boot -> gameplay -> render) on the hybrid store.

Extends verify_hybrid_tick from the gameplay tick to the full loop, including the numpy renderer. The product
-flip pattern: LOAD on the byte image (level load is a bridge-ward serialisation), then run gameplay on the
hybrid (named state off the image), and MATERIALISE before each render (the renderer needs a contiguous
image). If the rendered planes AND the DGROUP window match the ByteBackend reference every frame, the product
can run gameplay on the named field store end to end.

Like verify_hybrid_tick, when it diverges the first offset/plane names the next non-tick module still reaching
a named offset via raw .data -- the remaining work to a full product flip.

    python scripts/verify_hybrid_full.py [N_TICKS]     (default 120)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

DGROUP_BASE = 0x1A0F << 4
GAME_ROOT = str(ROOT / "assets")


def run(n_ticks: int, game_root: str = GAME_ROOT):
    """Returns (ok, message). ok=True means the whole boot->play->render path matched the reference."""
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.render import native_load_level_palette, native_render, native_sync_render_state
    from pre2.native.state import NativeGameState
    from pre2.native.vga import NativeVGA
    from pre2.views.dgroup_view import FieldBackend, HybridBackend, _named_map

    ref = native_cold_boot(game_root, level=0)          # LOAD on the image (both start from the same boot)
    hyb = NativeGameState(bytearray(ref.data))
    hb = HybridBackend(FieldBackend(hyb), hyb.data)     # swap to the field store for gameplay
    hyb.backend = hb
    dos_r, dos_h = NativeVGA(), NativeVGA()
    native_load_level_palette(ref, dos_r)
    native_load_level_palette(hyb, dos_h)

    for i in range(n_ticks):
        native_gameplay_frame(ref)
        native_gameplay_frame(hyb)
        hb.materialize(hyb.data)                        # fold named state back before ANY .data render read
        a = ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        b = hyb.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if a != b:
            off = next(k for k in range(0x10000) if a[k] != b[k])
            owner = _named_map().get(off, ("residue (unnamed)",))[0]
            return False, (f"{i} ticks clean, then DGROUP 0x{off:04X} ({owner}) diverged "
                           f"(ref 0x{a[off]:02X} != hybrid 0x{b[off]:02X}) -- a module reaches it via raw .data")

    native_sync_render_state(ref); native_sync_render_state(hyb)
    disp = ref.data[DGROUP_BASE + 0x2DD6] | (ref.data[DGROUP_BASE + 0x2DD7] << 8)
    rp, _ = native_render(ref, dos_r, disp, game_root=game_root, force_gameplay=True)
    hp, _ = native_render(hyb, dos_h, disp, game_root=game_root, force_gameplay=True)
    if b"".join(rp) != b"".join(hp):
        return False, "render planes diverged -- the render path reads a named offset via raw .data pre-materialise"
    return True, f"{n_ticks} ticks + render -- hybrid == reference (the product ran gameplay on the field store)"


def main() -> int:
    n_ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print(f"Phase 5 proof: boot -> {n_ticks} gameplay ticks -> render, on the hybrid store vs the reference ...")
    ok, msg = run(n_ticks)
    print(("  FULL: " if ok else "  FRONTIER: ") + msg)
    if ok:
        print("PROVEN: the product's boot->play->render path runs gameplay on the named field store.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
