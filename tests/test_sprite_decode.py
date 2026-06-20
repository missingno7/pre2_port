"""Verification tests for the recovered sprite-sheet decode (pre2.recovered.sprite_decode).

The transform was proven byte-for-byte equal to the original ASM (``1030:42F7``
local bank + ``1030:436A`` shared bank) by capturing the *load-time* witness — the
decompressed sprite sheet at level load together with the four planar VRAM cache
planes the ASM produced (``pre2/probes/capture_sprite_decode.py``). The mid-game
snapshot is not a faithful witness (the sheet RAM is freed and the cache is
over-drawn), so the capture is taken with the asset live at the decode boundary.

The golden below is the SHA-256 of the ASM cache bytes for every *meaningful* slot
(``code < 0x100`` local, or an in-bank shared sprite), in slot/plane order, so this
test locks the recovered transform against regressions without needing the VM.
Sentinel codes (``0xFFFF``) select unused slots whose ASM content is wrapped
garbage; they are reproduced byte-exact only by the live replacement (which reads
VM memory) and are excluded from this pure golden.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from pre2.recovered.sprite_decode import (
    SLOT_BYTES,
    demux_sprite,
    SharedSpriteBank,
    SpriteSheet,
    decode_sprite_cache,
)

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "sprite_decode"


def _load_fixture():
    sheet = SpriteSheet.from_bytes((FIX / "local_sheet.bin").read_bytes())
    bank = SharedSpriteBank((FIX / "shared_bank.bin").read_bytes())
    expected = json.loads((FIX / "expected.json").read_text())
    return sheet, bank, expected


def test_sprite_decode_matches_asm_witness():
    sheet, bank, expected = _load_fixture()
    cache = decode_sprite_cache(sheet, bank)

    h = hashlib.sha256()
    for slot, _code in expected["meaningful_slots"]:
        for plane in range(4):
            h.update(cache.slot(slot, plane))
    assert h.hexdigest() == expected["asm_meaningful_sha256"]


def test_local_and_shared_split():
    # 29 local (code < 0x100) + 182 in-bank shared sprites in this level.
    _sheet, _bank, expected = _load_fixture()
    n_local = sum(1 for _, c in expected["meaningful_slots"] if c < 0x100)
    n_shared = sum(1 for _, c in expected["meaningful_slots"] if c >= 0x100)
    assert (n_local, n_shared) == (expected["n_local"], expected["n_shared"])


def test_sentinel_slots_are_left_untouched():
    # Sentinel/out-of-bank codes select no real sprite; a fresh cache keeps them 0.
    sheet, bank, expected = _load_fixture()
    meaningful = {slot for slot, _ in expected["meaningful_slots"]}
    cache = decode_sprite_cache(sheet, bank)
    untouched = 0
    for slot in range(256):
        if slot in meaningful:
            continue
        untouched += 1
        for plane in range(4):
            assert cache.slot(slot, plane) == bytes(SLOT_BYTES)
    assert untouched == expected["n_sentinel"]


def test_demux_sprite_is_four_planes_of_thirtytwo_bytes():
    planes = demux_sprite(bytes(range(128)))
    assert len(planes) == 4
    assert all(len(p) == SLOT_BYTES for p in planes)
    assert planes[0] == bytes(range(0, 32))
    assert planes[3] == bytes(range(96, 128))


# ---- live replacement-adapter wiring (1030:42F7) ----------------------------
# A lightweight memory stub exercises the adapter's contract — the planar cache
# writes plus the [0x2CF1]/[0x2871]/[0x25CA] data side effects and the si/ds exit
# registers — without standing up the whole VM (the in-VM lockstep is proven
# separately by pre2/probes/verify_sprite_decode.py).
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE  # noqa: E402

from pre2.bridge.sprites import DATA_SEG, CACHE_OFF, read_slot  # noqa: E402
from pre2.recovered.sprite_decode import PIXEL_BASE, SPRITE_BYTES  # noqa: E402
from pre2 import replacements as R  # noqa: E402


class _FakeRegs:
    def __init__(self):
        self.ax = self.bx = self.cx = self.dx = 0
        self.si = self.di = self.bp = self.ip = 0
        self.ds = self.es = self.ss = 0


class _FakeMem:
    def __init__(self):
        self.data = bytearray(EGA_APERTURE + 4 * EGA_PLANE_STRIDE)
        self.ega_map_mask = 0x0F

    def rw(self, seg, off):
        a = (seg << 4) + off
        return self.data[a] | (self.data[a + 1] << 8)

    def ww(self, seg, off, val):
        a = (seg << 4) + off
        self.data[a] = val & 0xFF
        self.data[a + 1] = (val >> 8) & 0xFF


class _FakeDos:
    def __init__(self):
        self._seq_index = 0
        self._seq_regs = {}


class _FakeCPU:
    def __init__(self, mem):
        self.mem = mem
        self.s = _FakeRegs()
        self.pre2_dos = _FakeDos()
        self._ret = 0xBEEF

    def pop(self):
        return self._ret


def test_local_adapter_writes_cache_and_contract():
    mem = _FakeMem()
    src = 0x4000
    # selector -> multiplier 0 so sprite_sheet_segment == [0x2DD6] == src.
    mem.ww(DATA_SEG, 0x2DD6, src)
    mem.data[(DATA_SEG << 4) + 0x2D86] = 0
    mem.data[(DATA_SEG << 4) + 0x2D2C + 0] = 0
    # index table: slot 0 -> code 0 (local), slot 2 -> code 3 (local), rest sentinel.
    codes = [0xFFFF] * 256
    codes[0], codes[2] = 0, 3
    for i, c in enumerate(codes):
        mem.ww(src, 2 * i, c)
    # local pixel data: sprite n filled with byte (0x10 + n).
    for code in (0, 3):
        for k in range(SPRITE_BYTES):
            mem.data[(src << 4) + PIXEL_BASE + code * SPRITE_BYTES + k] = 0x10 + code

    cpu = _FakeCPU(mem)
    R.sprite_decode_local(cpu)

    # cache slots written, plane-demuxed.
    for slot, code in ((0, 0), (2, 3)):
        for plane in range(4):
            assert read_slot(mem, slot)[plane] == bytes([0x10 + code]) * 32
    # data side effects.
    assert mem.data[(DATA_SEG << 4) + 0x2CF1] == 0           # multiplier
    assert mem.rw(DATA_SEG, 0x2871) == src                   # bump/source seg
    idx = bytes(mem.data[(DATA_SEG << 4) + 0x25CA:(DATA_SEG << 4) + 0x25CA + PIXEL_BASE])
    assert idx == bytes(mem.data[(src << 4):(src << 4) + PIXEL_BASE])  # index-table copy
    # register exit contract: si = 0x200 + 0x80*nlocal (2 local sprites), ds = src.
    assert cpu.s.si == PIXEL_BASE + 0x80 * 2
    assert cpu.s.ds == src
    assert cpu.s.ip == 0xBEEF                                # near-ret target
    assert mem.ega_map_mask == 0x08                          # ASM demux exit state
