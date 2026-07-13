"""The DETACHABLE bridge that GENERATES the original DOS boot layout from the readable constants.

The inversion: ``pre2/native/boot_tables.py`` is now the SOURCE OF TRUTH (real Python values); this module
serialises those constants back into the original 64 KB DGROUP byte layout, so the native game can be verified
byte-for-byte against the DOS original. The shipped game cold-starts from the constants and never needs this;
ship without the bridge and the DOS memory layout is gone.

``generate_boot_dgroup()`` builds the image from the constants + ``_boot_residual.txt`` — the still-opaque
remainder (boot bitmap graphics + not-yet-decoded regions) as a named blob. Every table we decode into
``boot_tables`` moves those bytes OUT of the residual and into readable constants, so the residual is a shrinking
measure of "how much of the DOS fossil is left to untangle". ``verify_boot_layout()`` proves the generated image
equals ``boot_data.build_boot_memory()`` exactly — i.e. the constants can fully regenerate the DOS blob, so
``_DGROUP_Z`` is redundant.
"""
from __future__ import annotations

import base64
import zlib
from pathlib import Path

from pre2.native import boot_tables as T

DS_BASE = 0x1A0F << 4
DGROUP_LEN = 0x10000

# (constant, dgroup offset, encoder) — where each readable table lands in the original layout
_SINE_OFF, _COSINE_OFF, _HALF_OFF = 0x6F90, 0x7090, 0x7190
_JUMP_OFF, _ATTACK_OFF, _SCORE_OFF, _SCANCODE_OFF = 0x79CE, 0x7B04, 0xA343, 0x2301
_HITBOX_WX_OFF, _ANIM_ID_OFF, _ANIM_SEQ_OFF = 0x752A, 0x7B7F, 0x7CDF


def _enc_s8(vals) -> bytes:
    return bytes(v & 0xFF for v in vals)


def _enc_s16(vals) -> bytes:
    out = bytearray()
    for v in vals:
        v &= 0xFFFF
        out += bytes((v & 0xFF, (v >> 8) & 0xFF))
    return bytes(out)


_DIGIT_BASE, _DIGIT_STRIDE, _DIGIT_COUNT = 0xCE8A, 0x58, 9   # the 16x32 HUD digit sprites (1..9)


def _residual() -> bytearray:
    z = (Path(__file__).with_name("_boot_residual.txt")).read_text().strip()
    return bytearray(zlib.decompress(base64.b85decode(z)))


def _place_digit_sprites(img) -> None:
    """Regenerate the 9 HUD digit sprites from the committed PNG asset (the readable form of that artwork)."""
    from pre2.bridge.boot_graphics import png_to_region
    png = Path(__file__).with_name("assets") / "boot_digits.png"
    payload = png_to_region(str(png), _DIGIT_COUNT * 64, tile_w=16, tile_h=32, tiles_wide=_DIGIT_COUNT, gap=0)
    for k in range(_DIGIT_COUNT):
        off = _DIGIT_BASE + k * _DIGIT_STRIDE
        img[off:off + 64] = payload[k * 64:k * 64 + 64]


def generate_boot_dgroup() -> bytearray:
    """Regenerate the 64 KB DGROUP boot image from the readable constants + the residual blob."""
    img = _residual()
    img[_SINE_OFF:_SINE_OFF + 256] = _enc_s8(T.SINE_TABLE)
    img[_COSINE_OFF:_COSINE_OFF + 256] = _enc_s8(T.COSINE_TABLE)
    img[_HALF_OFF:_HALF_OFF + 64] = bytes(b for pair in T.SPRITE_HALF_EXTENTS for b in pair)
    img[_JUMP_OFF:_JUMP_OFF + 18] = _enc_s16(T.JUMP_IMPULSE)
    img[_ATTACK_OFF:_ATTACK_OFF + 20] = b"".join(
        bytes((p & 0xFF, (p >> 8) & 0xFF, sfx, v19, flag)) for (p, sfx, v19, flag) in T.ATTACK_PHASES)
    img[_SCORE_OFF:_SCORE_OFF + 34] = _enc_s16(T.SCORE_VALUES)
    img[_HITBOX_WX_OFF:_HITBOX_WX_OFF + 32] = bytes(T.HITBOX_HALF_WIDTHS)
    img[_ANIM_ID_OFF:_ANIM_ID_OFF + 24] = bytes(T.ANIM_STATE_IDS)
    img[_ANIM_SEQ_OFF:_ANIM_SEQ_OFF + 18] = _enc_s16(T.ANIM_SEQ_PTRS)
    sc = T.SCANCODE_CHARS.encode("latin1")
    img[_SCANCODE_OFF:_SCANCODE_OFF + len(sc)] = sc
    for off, text in T.RESOURCE_RECORDS:
        blob = text.encode("latin1") + b"\x00"
        img[off:off + len(blob)] = blob
    _place_digit_sprites(img)     # the HUD digit font, from its PNG asset
    return img


def constant_coverage() -> tuple[int, int]:
    """(bytes generated from readable constants, non-zero bytes still in the residual blob)."""
    covered = (256 + 256 + 64 + 18 + 20 + 34 + 32 + 24 + 18 + _DIGIT_COUNT * 64 + len(T.SCANCODE_CHARS)
               + sum(len(t) + 1 for _, t in T.RESOURCE_RECORDS))
    return covered, sum(1 for b in _residual() if b)


def verify_boot_layout() -> None:
    """Prove the constants regenerate the DOS boot image exactly (so _DGROUP_Z is redundant)."""
    from pre2.native.boot_data import build_boot_memory
    want = bytes(build_boot_memory()[DS_BASE:DS_BASE + DGROUP_LEN])
    got = bytes(generate_boot_dgroup())
    if got != want:
        off = next(k for k in range(DGROUP_LEN) if got[k] != want[k])
        raise AssertionError(f"generated boot DGROUP diverges at 0x{off:04X}: {got[off]:#04x} vs {want[off]:#04x}")


if __name__ == "__main__":
    verify_boot_layout()
    cov, resid = constant_coverage()
    print(f"boot layout OK — generated the DOS DGROUP from constants byte-exact; "
          f"{cov} bytes from readable constants, {resid} non-zero bytes still in the residual fossil")
