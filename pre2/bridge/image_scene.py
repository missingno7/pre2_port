"""The mode-13h IMAGE scenes (title / menu / titus / intro) — recovered from the asset, no VM framebuffer.

The 13h screens are linear 256-colour images copied to A000 by 1030:91A4 (+ 9090 for the title logo-top).
Each is a decoded asset (`unpack_sqz`, already recovered). The faithful renderer must NOT read the A000
framebuffer; instead it identifies which image is on screen (by fingerprinting the copy source at 91C0
against the known image assets) and re-renders it from the decoded asset.

Renderers per asset: PRESENT.SQZ = the title (background + logo-top, ``render_title_image``); the others
are a single 320x200 image at offset 0x300.
"""
from __future__ import annotations

import os

from pre2.codecs.sqz import unpack_sqz
from pre2.recovered.title_image import render_title_image, title_background

_TITLE = "PRESENT.SQZ"

_IMAGE_OFF = 0x300
_IMAGE_LEN = 64000

# the mode-13h image assets and how to render the displayed 320x200 linear image from the decoded asset
_RENDERERS = {
    "PRESENT.SQZ": render_title_image,                                  # title: background + logo-top
    "MENU.SQZ": lambda dec: dec[_IMAGE_OFF:_IMAGE_OFF + _IMAGE_LEN],    # single image @0x300
    "MENU2.SQZ": lambda dec: dec[_IMAGE_OFF:_IMAGE_OFF + _IMAGE_LEN],
    "TITUS.SQZ": lambda dec: dec[_IMAGE_OFF:_IMAGE_OFF + _IMAGE_LEN],
    "MOTIF.SQZ": lambda dec: dec[_IMAGE_OFF:_IMAGE_OFF + _IMAGE_LEN],
    "THEEND.SQZ": lambda dec: dec[_IMAGE_OFF:_IMAGE_OFF + _IMAGE_LEN],   # THE END (5034): 768-byte pal + 13h image
}

_decode_cache: dict = {}
_fingerprints: dict = {}


def _decoded(game_root: str, name: str) -> bytes:
    key = (game_root, name)
    if key not in _decode_cache:
        with open(os.path.join(game_root, name), "rb") as f:
            _decode_cache[key] = unpack_sqz(f.read())
    return _decode_cache[key]


def _fingerprint(game_root: str, name: str) -> bytes:
    key = (game_root, name)
    if key not in _fingerprints:
        dec = _decoded(game_root, name)
        _fingerprints[key] = dec[_IMAGE_OFF:_IMAGE_OFF + 256] if len(dec) >= _IMAGE_OFF + 256 else b""
    return _fingerprints[key]


def identify_image(source_image: bytes, game_root: str):
    """Identify which 13h image asset ``source_image`` (the first bytes the ASM copies to A000) is, by
    matching its first 256 bytes against the known image assets. Returns the asset name or None."""
    head = source_image[:256]
    for name in _RENDERERS:
        try:
            fp = _fingerprint(game_root, name)
            if any(fp) and head == fp:      # skip a DEGENERATE all-zero fingerprint (a black-topped image like
                return name                  # THEEND.SQZ can't be identified by its top row — and would match a
        except FileNotFoundError:            # blank screen; native renders THEEND explicitly, never by fingerprint)
            continue
    return None


def image_palette(name: str, game_root: str) -> bytes:
    """The 256-colour 6-bit VGA palette (768 bytes) at the start of a decoded 13h image asset.

    The fade routines ([[pre2.recovered.front_end_fade]]) ramp the DAC toward this palette."""
    return bytes(b & 0x3F for b in _decoded(game_root, name)[:_IMAGE_OFF])


def title_planar_image(name: str, game_root: str) -> tuple:
    """[asm resource-0xC, loaded via 0x10492 type-1] Decode a 16-colour PLANAR EGA image asset to its four
    8000-byte bitplanes + 16-entry 6-bit palette. The attract-title jungle screen (MENU2.SQZ) is NOT a 13h
    linear image (despite the stale _RENDERERS entry) — it is 4 concatenated EGA planes (40*200 bytes each)
    with a trailing 48-byte 6-bit palette (total 32048). Returns ``(planes: tuple[bytes*4], pal6: bytes[48])``."""
    dec = _decoded(game_root, name)
    plane_len = 320 * 200 // 8                       # 8000 bytes per EGA plane
    planes = tuple(bytes(dec[p * plane_len:(p + 1) * plane_len]) for p in range(4))
    pal6 = bytes(b & 0x3F for b in dec[4 * plane_len:4 * plane_len + 0x10 * 3])
    return planes, pal6


def render_image_scene(name: str, game_root: str, with_logo: bool = True) -> bytes:
    """Return the recovered 64000-byte linear 256-colour image for ``name`` (decoded from the asset).

    For the title (PRESENT.SQZ) the logo-top is overlaid only when ``with_logo`` (it is copied by a
    separate ASM pass, 9090, AFTER the background; during the fade-in only the background is on screen)."""
    dec = _decoded(game_root, name)
    if name == _TITLE and not with_logo:
        return bytes(title_background(dec))
    return bytes(_RENDERERS[name](dec))
