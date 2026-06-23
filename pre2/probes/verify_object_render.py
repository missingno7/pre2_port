"""TEMP probe: lockstep-verify Phase A (the sprite-renderer driver) vs the ASM.

At 1030:28BE (per drawn sprite, after position+clip) capture the ASM's computed
dest/byte_width/rows/shift/source, build the recovered plan from live memory, and
diff. Proves the cull/position/clip logic without touching pixels (Phase B).
Run: python -m pre2.probes.verify_object_render
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
from pre2.recovered.object_render import plan_sprite
from pre2.runtime import load_pre2_snapshot

SNAP = ROOT/"artifacts"/"snapshot_pre2_gameplay_20260621_185902"
BLIT = (0x1030, 0x28BE)
rt = load_pre2_snapshot(str(ROOT/"assets"/"pre2.exe"), str(SNAP), game_root=str(ROOT/"assets"),
                        native_replacements=True)
cpu = rt.cpu; cpu.trace_enabled = False
sb = enable_sound_blaster(rt); pic = rt.dos.pic
CHUNK = 15000; det_speed = CHUNK*30
vclock = {"base": perf_counter()}; tick_state = {"next": perf_counter()}
det_now = lambda: vclock["base"] + cpu.instruction_count/det_speed
sb.clock = det_now
stats = {"ok": 0, "bad": 0}
mism = []

def at_blit(c):
    mem = c.mem
    rec = B._rw(mem, B.DATA_SEG, B.VAR_CURSOR)
    spr = B.read_sprite(mem, rec)
    attr = B.read_attr(mem, spr.sprite_id)
    cam = B.read_camera(mem)
    cs = 0x1030
    asm = dict(
        dest_off=B._rw(mem, cs, 0x26F1),
        byte_width=B._rb(mem, cs, 0x26E4),
        rows=B._rb(mem, cs, 0x26EC),
        shift=B._rw(mem, cs, 0x26EA) & 7,
        src_off=B._rw(mem, cs, 0x26F3),
        src_seg=B._rw(mem, cs, 0x26F5),
    )
    d = plan_sprite(spr, attr, cam)
    if d is None:
        stats["bad"] += 1
        if len(mism) < 12:
            mism.append(f"id={spr.sprite_id:04X} ASM drew but recovered CULLED; asm={asm} spr=(x={spr.x:04X},y={spr.y:04X}) cam=(cx={cam.cam_x},cy={cam.cam_y})")
    else:
        got = dict(dest_off=d.dest_off, byte_width=d.byte_width, rows=d.rows,
                   shift=d.shift, src_off=d.src_off, src_seg=d.src_seg)
        if got == asm:
            stats["ok"] += 1
        else:
            stats["bad"] += 1
            if len(mism) < 12:
                diff = {k: (asm[k], got[k]) for k in asm if asm[k] != got[k]}
                mism.append(f"id={spr.sprite_id:04X} diff(asm,rec)={diff} spr=(x={spr.x:04X},y={spr.y:04X},w={attr.width},h={attr.height},xo={attr.x_off},yo={attr.y_off}) cam=(cx={cam.cam_x},cy={cam.cam_y},fine={cam.fine_scroll},rf={cam.row_factor},pg={cam.dest_page:04X},stride={cam.row_stride},gs={cam.global_shift})")
    interpret_current_instruction_without_hook(c)

cpu.replacement_hooks[BLIT] = at_blit
cpu.hook_names[BLIT] = "verify_obj_render"
deliver_scancode(rt, 0x4D, max_steps=200000)
for f in range(40):
    play._advance_demo_frame(rt, chunk_steps=CHUNK, sub_batch=3000, clock=det_now, pic=pic,
                             sound_blaster=sb, timer_irq=True, input_irq_steps=2_000_000, tick_state=tick_state)
    if f % 10 == 9: deliver_scancode(rt, 0x4D, max_steps=200000)
print(f"=== Phase A driver vs ASM: ok={stats['ok']} bad={stats['bad']} ===")
for m in mism:
    print("  " + m)
