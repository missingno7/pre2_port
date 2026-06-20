"""Verification tests for the recovered SQZ asset decompressor (pre2.codecs.sqz).

The decoder was proven byte-for-byte equal to the original ASM
(``1030:148F-16E3``) by capturing the live decompressor output the instant it
returns (``15EF``) and comparing — an EXACT 10528/10528 match on ``allfonts.sqz``
that exercises every decode branch (literal / short / ext3 / LONG / DEXT and all
four length sub-paths). The golden hash below is that verified-correct output, so
this test locks the recovered codec against regressions without needing the VM.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from pre2.codecs.sqz import SQZ_LZSS_MAGIC, unpack_sqz

ASSETS = pathlib.Path(__file__).resolve().parent.parent / "assets"

# sha256 of unpack_sqz(allfonts.sqz) == the original ASM's decompressed output.
ALLFONTS_SHA256 = "eedb134abdfdb2ba36698a0654d8e09b6af58c7ca3774a5ee9e3beea8254ec05"
# sha256 of unpack_sqz(keyb.sqz) == the original LZW ASM's 2048-byte output.
KEYB_SHA256 = "62901d89554f43ba8b99d1d4deb49e71885ab536d16f3edd6a8f5df712604520"

_LZW_NAMES = ("keyb", "castle", "present", "titus")


def _b44c_assets():
    if not ASSETS.is_dir():
        return []
    return sorted(p for p in ASSETS.glob("*.sqz") if p.read_bytes()[:10] == SQZ_LZSS_MAGIC)


def _lzw_assets():
    if not ASSETS.is_dir():
        return []
    return [ASSETS / f"{n}.sqz" for n in _LZW_NAMES if (ASSETS / f"{n}.sqz").exists()]


@pytest.mark.skipif(not (ASSETS / "allfonts.sqz").exists(), reason="game assets not present")
def test_allfonts_matches_asm_oracle():
    data = (ASSETS / "allfonts.sqz").read_bytes()
    out = unpack_sqz(data)
    assert len(out) == 10528
    assert hashlib.sha256(out).hexdigest() == ALLFONTS_SHA256


@pytest.mark.skipif(not _b44c_assets(), reason="game assets not present")
@pytest.mark.parametrize("path", _b44c_assets(), ids=lambda p: p.name)
def test_all_b44c_assets_decode_cleanly(path):
    raw = path.read_bytes()
    # Header invariant: compressed-length field (LE16 @ +10) == payload after the
    # 17-byte wrapper+header, i.e. the LZSS stream runs to end-of-file.
    comprlen = raw[10] | (raw[11] << 8)
    assert comprlen == len(raw) - 17
    out = unpack_sqz(raw)
    assert len(out) > 0


@pytest.mark.skipif(not (ASSETS / "keyb.sqz").exists(), reason="game assets not present")
def test_keyb_lzw_matches_asm_oracle():
    out = unpack_sqz((ASSETS / "keyb.sqz").read_bytes())
    assert len(out) == 2048
    assert hashlib.sha256(out).hexdigest() == KEYB_SHA256


@pytest.mark.skipif(not _lzw_assets(), reason="game assets not present")
@pytest.mark.parametrize("path", _lzw_assets(), ids=lambda p: p.name)
def test_all_lzw_assets_decode_cleanly(path):
    raw = path.read_bytes()
    assert (raw[1] & 0xF0) == 0x10  # LZW header magic
    expected = ((raw[0] & 15) << 16) | raw[2] | (raw[3] << 8)  # 20-bit size field
    out = unpack_sqz(raw)
    assert len(out) == expected


@pytest.mark.skipif(not (ASSETS / "pre2.exe").exists(), reason="game assets not present")
def test_sqz_checkpoint_matches_asm_in_vm():
    """Integration: the in-VM checkpoint sees the recovered codec == ASM, live.

    Cold-boots the real binary until the title screen decompresses ALLFONTS.SQZ
    and asserts the checkpoint verified it against the original ASM with zero
    divergence. Slow (runs the VM); skipped when assets are absent.
    """
    from dos_re.interrupts import deliver_interrupt, deliver_scancode
    from pre2.codecs.sqz_hook import install_sqz_decode_checkpoint
    from pre2.runtime import create_pre2_runtime

    rt = create_pre2_runtime(str(ASSETS / "pre2.exe"), game_root=str(ASSETS), fast_adlib=True)
    stats = install_sqz_decode_checkpoint(rt, raise_on_divergence=True)
    makes = {60: 0x1C}
    breaks = {110: 0x9C}
    for b in range(420, 3200, 120):
        makes[b] = 0x1C
        breaks[b + 50] = 0x9C
    for frame in range(600):
        if frame in makes:
            deliver_scancode(rt, makes[frame], max_steps=2_000_000)
        if frame in breaks:
            deliver_scancode(rt, breaks[frame], max_steps=2_000_000)
        for _ in range(4000):
            rt.cpu.step()
        deliver_interrupt(rt, 0x08, max_steps=2_000_000)
        if stats.verified >= 1:
            break
    assert stats.verified >= 1
    assert stats.diverged == []
