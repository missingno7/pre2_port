"""AUDIT: every runtime write OUTSIDE DGROUP during gameplay — the digest-blind state class (the class
that hid the earthquake map-restore bug: the gameplay digest covers the 64KB DGROUP only).

Watches (a) the [0x2DDA] level-map segment, (b) the [0x2875] trigger bank, (c) true CS-locals
(code-segment addresses below the DGROUP overlap), while replaying an input demo through the hybrid VM.
Collects writer CS:IPs (recorded post-instruction, so sites read ~-2..-5 from the listed ip).

RESULT on demo 210723 (2400 frames: proximity fires + death + respawn + walk-back, the heaviest known
out-of-DGROUP traffic), 2026-07-05:
  MAP : 52f0 (52D2 restore rep-movsb), 5453 + 547c (5427 collapse shift + reveal) — ALL RECOVERED.
  BANK: no runtime writers (load-time only, 41CA).
  CS  : 07b2/07b7/07bd timer-ISR internals (native owns its timing model); 30xx curtain/iris render
        scratch; 45e9 + 4779 = cs:[0x45AE] HUD-redraw dirty counter (render bookkeeping — native redraws
        the HUD statelessly); 563a/5642 camera-follow CS-locals (RECOVERED, camera_scroll._wb_cs).
  => every runtime write outside DGROUP is identified: recovered, or render/ISR plumbing native owns.

    python pre2/probes/audit_outside_dgroup_writers.py [demo_dir] [n_frames]
"""
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
from collections import Counter
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.runtime import load_pre2_snapshot
from pre2.bridge.timing_fastforward import advance_frame_fast

DEMO = sys.argv[1] if len(sys.argv) > 1 else 'artifacts/demo_pre2_20260704_210723'
N = int(sys.argv[2]) if len(sys.argv) > 2 else 2400                                   # past the respawn (~tick 653 -> frame ~2300 at 2142 chunk pacing)
DS_SEG = 0x1A0F
pb = InputDemoPlayback.load(DEMO)
rt = load_pre2_snapshot('assets/pre2.exe', pb.snapshot_path(), game_root='assets', native_replacements=True)
cpu = rt.cpu; cpu.trace_enabled = False; mem = cpu.mem
chunk = 2142; det_speed = chunk * 70
det = lambda: cpu.instruction_count / det_speed
rt.dos.time_source = det
sb = enable_sound_blaster(rt); sb.clock = det
tick = {'next': 0.0}

d = mem.data
map_seg = d[(DS_SEG << 4) + 0x2DDA] | (d[(DS_SEG << 4) + 0x2DDB] << 8)
bank_seg = d[(DS_SEG << 4) + 0x2875] | (d[(DS_SEG << 4) + 0x2876] << 8)
MAP_LO, MAP_HI = map_seg << 4, (map_seg << 4) + 0x10000
BANK_LO, BANK_HI = bank_seg << 4, (bank_seg << 4) + 0x400
CS_LO, CS_HI = 0x1030 << 4, (0x1030 << 4) + 0x10000
DG_LO, DG_HI = DS_SEG << 4, (DS_SEG << 4) + 0x10000
print(f'map seg {map_seg:#06x}  bank seg {bank_seg:#06x}')

writers = Counter()          # (region, cs, ip) -> count
def watch(addr, old, new):
    if old == new:
        return
    if MAP_LO <= addr < MAP_HI and not (DG_LO <= addr < DG_HI):
        region = 'MAP'
    elif BANK_LO <= addr < BANK_HI:
        region = 'BANK'
    elif CS_LO <= addr < min(CS_HI, DG_LO):
        region = 'CS'   # true code-segment locals only (the CS window overlaps DGROUP's first 0x6210 bytes)
    else:
        return
    s = cpu.s
    writers[(region, s.cs & 0xFFFF, s.ip & 0xFFFF)] += 1

mem.write_watchers.append(watch)
for f in range(N):
    pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2_000_000))
    advance_frame_fast(rt, chunk_steps=chunk, sub_batch=2000, clock=det, pic=rt.dos.pic,
                       sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000,
                       tick_state=tick, det_speed=det_speed,
                       active_fraction=rt.dos.vga_retrace_active_fraction, base=0.0)
    if sb.pcm_out: sb.pcm_out.clear()
mem.write_watchers.clear()

print(f'\nruntime writers OUTSIDE DGROUP over {N} frames (region, cs:ip, writes):')
for (region, cs, ip), n in sorted(writers.items()):
    print(f'  {region:4s} {cs:04x}:{ip:04x}  x{n}')
if not writers:
    print('  (none)')
