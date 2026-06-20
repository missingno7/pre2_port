"""Layer-1 audio asset verification (decode/model at asset boundaries).

Every PRE2 ``.TRK`` decompresses (verified SQZ codec) to a standard ProTracker M.K.
module whose layout closes exactly; ``SAMPLE.SQZ`` decompresses to the 60768-byte
PCM SFX bank. This is the asset-boundary contract — it does not touch the (still-ASM)
tracker player / mixer. Skipped when the original assets are absent.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from pre2.codecs import audio as A  # noqa: E402
from pre2.codecs.sqz import unpack_sqz  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
TRKS = sorted(f for f in os.listdir(ASSETS) if f.lower().endswith(".trk")) if os.path.isdir(ASSETS) else []

pytestmark = pytest.mark.skipif(not TRKS, reason="original PRE2 assets not present")

_FIXED = (A.MOD_TITLE_LEN + A.MOD_NUM_SAMPLES * A.MOD_SAMPLE_HDR_LEN + 2
          + A.MOD_ORDER_LEN + 4)


@pytest.mark.parametrize("name", TRKS)
def test_trk_is_a_well_formed_protracker_module(name):
    raw = open(os.path.join(ASSETS, name), "rb").read()
    decoded = unpack_sqz(raw)
    mod = A.parse_mod(decoded)

    assert mod.signature == "M.K."                 # PRE2 tracks are 4ch M.K.
    assert len(mod.samples) == A.MOD_NUM_SAMPLES    # always 31 headers
    assert all(0 <= s.volume <= 64 for s in mod.samples)
    assert all(s.length >= 0 and s.loop_len >= 0 for s in mod.samples)

    # the layout closes exactly against the (verified) SQZ-decoded length
    computed = (_FIXED + mod.num_patterns * A.MOD_PATTERN_BYTES
                + sum(s.length for s in mod.samples))
    assert computed == len(decoded), f"{name}: layout {computed} != decoded {len(decoded)}"


def test_load_trk_round_trips_through_the_codec():
    name = TRKS[0]
    raw = open(os.path.join(ASSETS, name), "rb").read()
    assert A.load_trk(raw) == A.parse_mod(unpack_sqz(raw))


def test_sample_sqz_is_the_pcm_sfx_bank():
    path = os.path.join(ASSETS, "sample.sqz")
    if not os.path.isfile(path):
        pytest.skip("sample.sqz absent")
    assert len(unpack_sqz(open(path, "rb").read())) == A.SFX_BANK_BYTES
