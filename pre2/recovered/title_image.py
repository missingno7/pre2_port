"""The PREHISTORIK 2 title 13h image (1030:91A4 background + 1030:9090 logo-top) — two layers, one asset.

The title screen is a mode-13h linear 256-colour image built from ONE asset (PRESENT.SQZ, decoded by the
recovered ``unpack_sqz`` — LZW), which stores TWO images plus a palette:
  * offset 0x0000 .. 0x0300 : the 256-colour palette (768 bytes, 6-bit VGA components)
  * offset 0x0300 .. +64000  : the BACKGROUND (320x200, the jungle/volcano scene)
  * offset 0x10300 .. +29760 : the LOGO-TOP (320x93, the "PREHISTORIK 2" logo over the upper scene)

The ASM composites them with two copies: 1030:91A4 (`rep movsw` 64000 B from asset+0x300 -> A000 = the
full background), then 1030:9090 (`rep movsw` 29760 B from asset+0x10300 -> A000:0 = overlay the top 93
rows with the logo). The recovered render reproduces that composite from the decoded asset — proven Δ=0
(100%) vs the displayed title framebuffer.

The base and the logo-top are kept as SEPARATE leaves (the original separates them): the enhanced
renderer can present the logo as its own layer.
"""
from __future__ import annotations

from pre2.islands import oracle_link

_PALETTE_LEN = 0x300
_IMAGE_OFF = 0x300          # [asm 91A4 / 926E: source = asset + 0x30 paragraphs]
_IMAGE_LEN = 64000          # 320 x 200
_LOGO_OFF = 0x10300         # [asm 90AF: ax = asset_seg + 0x1030 paragraphs = +0x10300 bytes]
_LOGO_LEN = 29760           # [asm 90BD: cx=0x3A20 words] = 320 x 93


def title_palette(asset: bytes) -> bytes:
    """The 768-byte 6-bit VGA palette at the start of the decoded asset."""
    return asset[:_PALETTE_LEN]


@oracle_link("1030:91A4",
             "title-image BACKGROUND copy: rep-movsw 64000 bytes (320x200) from the decoded title asset "
             "(PRESENT.SQZ via unpack_sqz) at offset 0x300 to the mode-13h A000 linear framebuffer.",
             "VERIFIED", merge_target="render_scene")
def title_background(asset: bytes) -> bytes:
    """The 64000-byte background image (320x200 linear) at asset offset 0x300."""
    return asset[_IMAGE_OFF:_IMAGE_OFF + _IMAGE_LEN]


@oracle_link("1030:9090",
             "title-image LOGO-TOP overlay: rep-movsw 29760 bytes (320x93) from the decoded title asset "
             "at offset 0x10300 to A000:0 — overlays the top 93 rows of the background with the "
             "'PREHISTORIK 2' logo. Same asset as the background (a second image in PRESENT.SQZ).",
             "VERIFIED", merge_target="render_scene")
def title_logo_top(asset: bytes) -> bytes:
    """The 29760-byte logo-top image (320x93 linear) at asset offset 0x10300."""
    return asset[_LOGO_OFF:_LOGO_OFF + _LOGO_LEN]


def render_title_image(asset: bytes) -> bytes:
    """Compose the full 64000-byte title linear image: the background with the logo-top overlaid on the
    top 93 rows ([asm 91A4 then 9090])."""
    img = bytearray(title_background(asset))
    top = title_logo_top(asset)
    img[:len(top)] = top
    return bytes(img)
