"""Regenerate the scan_camera_targets / camera_script_interp golden fixtures from the live VM.

The originals were captured recording the RECOVERED function's reads — which missed the unconditional
[0xA423] read the ASM makes at 8188 (the _target_collision id-check bug). After fixing that, the fn reads
[0xA423], which the old sparse fixtures lack. This re-captures: snapshot the full DGROUP at each routine's
ASM entry across the gorilla demo, run the (fixed) recovered fn with a recording reader over the snapshot,
and emit diverse cases (deduped by the write-offset shape so collision-response firings are included).

    python -m pre2.probes.regen_object_spawn_fixtures
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.recovered.object_spawn import scan_camera_targets, camera_script_interp
from pre2.runtime import load_pre2_snapshot
import play

DS = 0x1A0F; DS_BASE = DS << 4
DEMO = "artifacts/demo_pre2_20260703_092759"
ROUTINES = {"scan_camera_targets": (0x80DE, scan_camera_targets),
            "camera_script_interp": (0x7534, camera_script_interp)}
MAX_CASES = 14


def _run_recording(dgroup, fn):
    rb_rec, rw_rec, tile_rec = {}, {}, {}

    def rb(o):
        o &= 0xFFFF; v = dgroup[o]; rb_rec[o] = v; return v

    def rw(o):
        o &= 0xFFFF; v = dgroup[o] | (dgroup[(o + 1) & 0xFFFF] << 8); rw_rec[o] = v; return v

    def tile(o):
        o &= 0xFFFF; v = dgroup[o]; tile_rec[o] = v; return v

    writes = fn(rb, rw) if fn.__code__.co_argcount == 2 else fn(rb, rw, tile)
    return rb_rec, rw_rec, tile_rec, writes


def main():
    pb = InputDemoPlayback.load(str(ROOT / DEMO))
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 2142)); hz = int(meta.get("present_hz", 70))
    rt = load_pre2_snapshot(str(ROOT / "assets/pre2.exe"), pb.snapshot_path(),
                            game_root=str(ROOT / "assets"), native_replacements=False)
    cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
    det = lambda: cpu.instruction_count / (chunk * hz)
    rt.dos.time_source = det
    sb = enable_sound_blaster(rt); sb.clock = det
    tick = {"next": 0.0}; frame = [0]

    hit_cases = {name: {} for name in ROUTINES}    # keyed by content hash — collision-response firings
    idle_cases = {name: {} for name in ROUTINES}   # no-write cases
    orig = cpu.step

    def sstep():
        s = cpu.s
        if (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
            ip = s.ip & 0xFFFF
            for name, (entry, fn) in ROUTINES.items():
                if ip == entry:
                    dgroup = bytes(mem.data[DS_BASE:DS_BASE + 0x10000])
                    rb_rec, rw_rec, tile_rec, writes = _run_recording(dgroup, fn)
                    case = {
                        "rb": {str(k): v for k, v in sorted(rb_rec.items())},
                        "rw": {str(k): v for k, v in sorted(rw_rec.items())},
                        "tile": {str(k): v for k, v in sorted(tile_rec.items())},
                        "writes": {str(k): list(v) for k, v in writes.items()},
                    }
                    key = json.dumps(case, sort_keys=True)
                    bucket = hit_cases[name] if writes else idle_cases[name]
                    bucket.setdefault(key, case)
        orig()

    cpu.step = sstep
    while not pb.finished(frame[0]):
        pb.apply_to_runtime(frame[0], rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
        play._advance_demo_frame(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                                 sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick)
        if sb.pcm_out:
            sb.pcm_out.clear()
        frame[0] += 1

    outdir = ROOT / "tests" / "fixtures" / "object_spawn"
    for name in ROUTINES:
        hits = list(hit_cases[name].values())
        idles = list(idle_cases[name].values())
        cases = hits + idles[:max(2, MAX_CASES - len(hits))]      # keep ALL collision firings + a few idle
        (outdir / f"{name}.json").write_text(json.dumps(cases, indent=0))
        print(f"{name}: {len(cases)} cases ({len(hits)} collision-response, {len(cases)-len(hits)} idle) -> {name}.json")


if __name__ == "__main__":
    main()
