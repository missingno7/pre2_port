"""Hunt for a STABLE per-object handle in the active-list record (18 bytes), so the enhanced compositor can
match objects across source frames despite (a) animation changing sprite_id/base_id and (b) list compaction
changing the slot index. Tracks the moving enemy on witness 170717 by world-position continuity and dumps
its full record each frame; bytes that stay constant while x/y change are handle candidates."""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.object_render import LIST_BASE, LIST_TOP, RECORD_BYTES
from pre2.runtime import load_pre2_snapshot

_6772 = (0x1030, 0x6772)
SEG = 0x1030 << 4


def run(snap, frames=10):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    ds = 6428 * 70
    dos.time_source = lambda: cpu.instruction_count / ds
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    out = []
    orig = cpu.replacement_hooks.get(_6772)
    from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook

    def hook(c):
        recs = []   # (slot, full 18 bytes) for slots with a plausible sprite id (low byte set, not 0xFFFF)
        for i, off in enumerate(range(LIST_TOP, LIST_BASE - 1, -RECORD_BYTES)):
            b = bytes(c.mem.data[SEG + off: SEG + off + RECORD_BYTES])
            sid = b[4] | (b[5] << 8)
            x = b[0] | (b[1] << 8)
            # the moving enemy lives around world_x 1490..1600 with a low sprite id
            if sid != 0xFFFF and 1480 <= x <= 1600 and (sid & 0x1FFF) < 0x400:
                recs.append((i, b))
        out.append(recs)
        return orig(c) if orig else interpret_current_instruction_without_hook(c)
    cpu.replacement_hooks[_6772] = hook

    def pump():
        now = cpu.instruction_count / ds
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0); tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            n = pic.acknowledge()
            if n is None:
                break
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=2_000_000)
            g += 1

    g = 0
    while len(out) < frames and g < ds * 40:
        if cpu.instruction_count % 2000 == 0:
            pump()
        cpu.step(); g += 1
    return out


def main():
    fr = run("artifacts/snapshot_pre2_20260625_170717")
    print("per frame: the moving enemy's record(s) [slot] bytes 0..17 (x@0:2, y@2:4, sid@4:6, life@17):")
    for fi, recs in enumerate(fr):
        for slot, b in recs:
            print(f"  f{fi} slot{slot}: " + " ".join(f"{i}:{x:02x}" for i, x in enumerate(b)))
    # which byte offsets stay constant across frames (handle candidates)? compare the single tracked record
    seqs = [recs[0][1] for recs in fr if recs]
    if len(seqs) >= 3:
        print("\nbyte-offset stability across frames (offset: distinct values):")
        for off in range(RECORD_BYTES):
            vals = sorted({s[off] for s in seqs})
            const = " <-- CONSTANT" if len(vals) == 1 else ""
            print(f"  off {off:2d}: {[hex(v) for v in vals]}{const}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
