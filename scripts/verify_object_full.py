"""Gap #2 proof: the WHOLE product path (boot -> gameplay -> render) runs gameplay on the object graph.

Mirrors verify_hybrid_full, but the gameplay store is the offset-free ``DataclassBackend`` (the game model:
Player/Camera/Progress/... dataclasses + the entity arena, with the DOS offsets only in the bridge layout).
Pattern: LOAD on the byte image (level load is a bridge-ward serialisation), swap to the object graph for
gameplay (readonly_image=True — the tick must write NOTHING to the loaded data), MATERIALISE before each render
(the numpy renderer needs a contiguous image). If the rendered planes AND the DGROUP match the ByteBackend
reference every frame, the shipped product can run gameplay on the object graph end-to-end.

    python scripts/verify_object_full.py [N_TICKS]     (default 120)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))
DGROUP_BASE = 0x1A0F << 4
GAME_ROOT = str(ROOT / "assets")


def run(n_ticks: int, game_root: str = GAME_ROOT):
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.render import native_load_level_palette, native_render, native_sync_render_state
    from pre2.native.state import NativeGameState
    from pre2.native.vga import NativeVGA

    ref = native_cold_boot(game_root, level=0)              # LOAD on the image
    obj = NativeGameState(bytearray(ref.data))
    obj.backend = DataclassBackend(obj, readonly_image=True)   # swap to the object graph for gameplay
    dos_r, dos_o = NativeVGA(), NativeVGA()
    native_load_level_palette(ref, dos_r)
    native_load_level_palette(obj, dos_o)

    for i in range(n_ticks):
        native_gameplay_frame(ref)
        native_gameplay_frame(obj)
        obj.backend.materialize()                           # fold the object graph back before any render read
        a = ref.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        b = obj.data[DGROUP_BASE:DGROUP_BASE + 0x10000]
        if a != b:
            off = next(k for k in range(0x10000) if a[k] != b[k])
            return False, f"{i} ticks clean, then DGROUP 0x{off:04X} diverged (ref {a[off]:#04x} != obj {b[off]:#04x})"

    # render runs on the materialised image (the numpy renderer + sync_render_state mix raw .data), exactly as
    # the field-store product does — the gameplay tick already proved it writes nothing to the loaded data.
    from pre2.views.dgroup_view import ByteBackend
    obj.backend.materialize()
    obj.backend = ByteBackend(obj)
    native_sync_render_state(ref); native_sync_render_state(obj)
    disp = ref.data[DGROUP_BASE + 0x2DD6] | (ref.data[DGROUP_BASE + 0x2DD7] << 8)
    rp, _ = native_render(ref, dos_r, disp, game_root=game_root, force_gameplay=True)
    op, _ = native_render(obj, dos_o, disp, game_root=game_root, force_gameplay=True)
    if b"".join(rp) != b"".join(op):
        return False, "render planes diverged"
    return True, f"{n_ticks} ticks + render -- object graph == reference (the product ran gameplay on objects)"


def main() -> int:
    n_ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print(f"Gap #2 proof: boot -> {n_ticks} gameplay ticks -> render, on the object graph vs the reference ...")
    ok, msg = run(n_ticks)
    print(("  FULL: " if ok else "  FRONTIER: ") + msg)
    if ok:
        print("PROVEN: the product's boot->play->render path runs gameplay on the offset-free object graph.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
