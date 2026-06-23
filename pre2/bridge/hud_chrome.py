"""Load the static HUD chrome from its persistent source asset (ALLFONTS.SQZ).

The HUD chrome lives entirely in ``ALLFONTS.SQZ`` (verified byte-exact vs the in-VM HUD strip on a
mid-game snapshot, and corroborated by the blues_p2 ``pre2_editor`` gameplay runtime). The decoded
blob holds, in order: ``[0 .. bg_off)`` other glyph data, ``[bg_off .. bg_off+bg_len)`` the 320x23
status-bar PANEL bitmap (planar — the gray panel + caveman face icon + the ``LIVES:``/``SCORE:``/
``ENERGY:`` labels + the boss-meter box), then the 16x12 HUD glyph font at ``0x1610`` (glyph 0).

The in-VM bar at ``0x252B:0x0B48`` is a *transient* runtime copy (reused after the level-start
blit), so it cannot be read off an arbitrary snapshot — ``ALLFONTS.SQZ`` is the persistent source.

Layout (from the editor reference, verified against PRE2.EXE):

    bg_off = 41 * 48          # 0x07B0  panel bitmap start (right before the font glyphs)
    bg_len = 320 * 23 // 2    # 0x0E60  = 4 planes x 0x398
    font glyphs at 0x1610     # 16x12 (0x60 bytes) each, glyph 0 = digit '0'

Asset filename / offsets live here (bridge); the recovered renderer consumes the typed
:class:`~pre2.recovered.render_model.HudChromeAsset`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pre2.codecs.sqz import unpack_sqz
from pre2.recovered.render_model import HudChromeAsset

_CHROME_SQZ = "ALLFONTS.SQZ"
_BG_OFF = 41 * 48          # 0x07B0 — status-bar panel bitmap offset within the decoded blob
_BG_LEN = 320 * 23 // 2    # 0x0E60 — 320x23 planar, 4 planes x 0x398


@lru_cache(maxsize=4)
def _decode(path: str) -> bytes:
    return unpack_sqz(Path(path).read_bytes())


def load_hud_chrome(game_root) -> HudChromeAsset:
    """Decode the persistent HUD chrome (panel bitmap + glyph font) from ``ALLFONTS.SQZ`` under
    ``game_root``. Both pieces come from this one asset, so the HUD renders from any snapshot. Cached.

    The font is the whole decoded blob (the glyph blit indexes it from offset 0, glyphs at 0x1610)."""
    dec = _decode(str(Path(game_root) / _CHROME_SQZ))
    return HudChromeAsset(bar=dec[_BG_OFF:_BG_OFF + _BG_LEN], font=dec)
