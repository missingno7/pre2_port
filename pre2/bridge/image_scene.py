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
            if head == _fingerprint(game_root, name):
                return name
        except FileNotFoundError:
            continue
    return None


def render_image_scene(name: str, game_root: str, with_logo: bool = True) -> bytes:
    """Return the recovered 64000-byte linear 256-colour image for ``name`` (decoded from the asset).

    For the title (PRESENT.SQZ) the logo-top is overlaid only when ``with_logo`` (it is copied by a
    separate ASM pass, 9090, AFTER the background; during the fade-in only the background is on screen)."""
    dec = _decoded(game_root, name)
    if name == _TITLE and not with_logo:
        return bytes(title_background(dec))
    return bytes(_RENDERERS[name](dec))
