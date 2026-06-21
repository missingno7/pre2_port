"""TEMP probe: lockstep-verify Phase B (the planar blit) vs the ASM.

At 26FA entry, plan + paint the recovered planes from a copy of entry VRAM; let the
ASM run to 2DF9; diff the four planes over the sprites' dest regions. Reports total
mismatched bytes split by clipped vs non-clipped sprite (clipped = the 2CEA variant,
not yet implemented). Run: python -m pre2.probes.verify_object_blit
"""
import sys
from pathlib import Path
from time import perf_counter
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/"scripts"))
import play
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_scancode
from pre2.bridge import object_render as B
from pre2.recovered.object_render import plan_sprite, paint_sprite
from pre2.runtime import load_pre2_snapshot

SNAP = ROOT/"artifacts"/"snapshot_pre2_20260621_185902"
ENTRY = (0x1030, 0x26FA); EXITP = (0x1030, 0x2DF9)
rt = load_pre2_snapshot(str(ROOT/"assets"/"pre2.exe"), str(SNAP), game_root=str(ROOT/"assets"),
                        native_replacements=True)
cpu = rt.cpu; cpu.trace_enabled = False
sb = enable_sound_blaster(rt); pic = rt.dos.pic
CHUNK = 15000; det_speed = CHUNK*30
vclock = {"base": perf_counter()}; tick_state = {"next": perf_counter()}
det_now = lambda: vclock["base"] + cpu.instruction_count/det_speed
sb.clock = det_now
pend = []
agg = {"frames": 0, "nc_sprites": 0, "clip_sprites": 0, "nc_bad": 0, "clip_bad": 0}
examples = []


def _region(draw, stride):
    for r in range(draw.rows):
        base = (draw.dest_off + r * stride) & 0xFFFF
        for c in range(draw.byte_width + 1):
            yield (base + c) & 0xFFFF


def at_entry(c):
    mem = c.mem
    cam = B.read_camera(mem)
    rec = B.read_planes(mem)
    draws = []
    for off, spr in B.read_active_list(mem):
        if spr.sprite_id == 0xFFFF:
            continue
        attr = B.read_attr(mem, spr.sprite_id)
        d = plan_sprite(spr, attr, cam)
        if d is None:
            continue
        draws.append(d)
        block = d.src_bw * d.rows
        src = B.read_source(mem, d.src_seg, d.src_off, 6 * block + 32)
        paint_sprite(rec, d, src, cam.row_stride)
    pend.append((rec, draws, set(), cam.row_stride))
    interpret_current_instruction_without_hook(c)


def at_exit(c):
    if not pend:
        interpret_current_instruction_without_hook(c); return
    rec, draws, clip_off, stride = pend.pop()
    asm = B.read_planes(c.mem)
    agg["frames"] += 1
    # union of all written offsets (deduplicated) — the true plane diff, no overlap
    # double-counting and no carry-wrap mis-attribution.
    union = set()
    for d in draws:
        union.update(_region(d, stride))
        if d.clipped:
            agg["clip_sprites"] += 1
        else:
            agg["nc_sprites"] += 1
        agg.setdefault("modes", {})[d.mode] = agg.setdefault("modes", {}).get(d.mode, 0) + 1
        agg.setdefault("shifts", set()).add(d.shift)
    for off in union:
        for p in range(4):
            if asm[p][off] != rec[p][off]:
                agg["nc_bad"] += 1
                if len(examples) < 10:
                    examples.append(f"off={off:04X} plane{p} asm={asm[p][off]:02X} rec={rec[p][off]:02X}")
    interpret_current_instruction_without_hook(c)


cpu.replacement_hooks[ENTRY] = at_entry; cpu.hook_names[ENTRY] = "obl_entry"
cpu.replacement_hooks[EXITP] = at_exit; cpu.hook_names[EXITP] = "obl_exit"
deliver_scancode(rt, 0x4D, max_steps=200000)
for f in range(90):
    play._advance_demo_frame(rt, chunk_steps=CHUNK, sub_batch=3000, clock=det_now, pic=pic,
                             sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick_state)
    if f % 6 == 5: deliver_scancode(rt, 0x4D, max_steps=200000)
print(f"=== Phase B blit vs ASM ({agg['frames']} renderer calls) ===")
print(f"  non-clipped sprites: {agg['nc_sprites']}  mismatched bytes: {agg['nc_bad']}")
print(f"  clipped sprites:     {agg['clip_sprites']}  mismatched bytes: {agg['clip_bad']}")
for e in examples:
    print("  " + e)
