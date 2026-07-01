"""Verification tests for the recovered front-end DAC fades (``pre2.recovered.front_end_fade``).

The two fade routines — ``1030:919F`` (13h title fade-IN from black to the asset palette) and
``1030:9286`` (fade-OUT to black) — were proven **byte-for-byte equal to the original ASM** by
capturing the VM's DAC state at every retrace wait (``44CD``) through the whole title sequence on
the near-coldstart demo and diffing it against the recovered per-retrace-frame sequence:

    TITUS   fade-IN  : 31 retrace frames  — OK byte-exact
    TITUS   fade-OUT : 16 retrace frames  — OK byte-exact
    PRESENT fade-IN  : 256 retrace frames — OK byte-exact

(The DAC round-trips 6-bit exactly — ``_dac8(v6) >> 2 == v6`` — so the comparison is in 6-bit DAC
units.) The goldens below are the SHA-1 of the concatenated 6-bit DAC snapshots the recovered
function produces for the real title-asset palettes; they were captured from the run that matched
the VM, so this test locks the transform against regressions with no VM in the loop.

``cs:[9153]`` = the DAC entry count each routine ramps: TITUS uses ``cl=0x10`` (16 entries),
PREHISTORIK-2 (PRESENT.SQZ) uses ``cl=0xFF`` (255). The held-image palette the fade-OUT starts
from is the fade-IN's converged final frame.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pre2.codecs.sqz import unpack_sqz
from pre2.recovered.front_end_fade import fade_in_frames, fade_out_frames, palette_morph_frames

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ACE7_FIXTURE = ROOT / "tests" / "fixtures" / "present_morph_ace7.bin"

# SHA-1 of b"".join(frames) — each frame is 0x300 bytes of 6-bit DAC (256 entries × RGB).
# Captured from the recovered function on the run that matched the VM DAC sequence byte-for-byte.
GOLD_TITUS_IN = "6b69c0c534780982b245103d07ca6ab49be5220f"
GOLD_TITUS_OUT = "5feb0f97b4bafe1b32c2b3f45a7837e83192b8ef"
GOLD_PRESENT_IN = "136545b7768e94df7beab364027fac70705d09e1"

_BLACK = bytes(0x300)

pytestmark = pytest.mark.skipif(
    not (ASSETS / "TITUS.SQZ").exists() or not (ASSETS / "PRESENT.SQZ").exists(),
    reason="original PRE2 title assets not present",
)


def _palette6(name: str) -> bytes:
    """The 6-bit 768-byte palette at the start of a decoded 13h image asset."""
    dec = unpack_sqz((ASSETS / name).read_bytes())
    return bytes(b & 0x3F for b in dec[:0x300])


def _sha1(frames) -> str:
    return hashlib.sha1(b"".join(frames)).hexdigest()


def test_titus_fade_in_byte_exact():
    frames = fade_in_frames(_palette6("TITUS.SQZ"), 0x10)
    assert len(frames) == 31
    assert frames[0] == _BLACK                      # the DAC starts black (cleared before 919F)
    # only the first n=0x10 entries (48 bytes) are ramped; they converge exactly to the target
    # (max component 0x3C < the 0x3D clamp). Entries 16..255 are left black by this n.
    assert frames[-1][:48] == _palette6("TITUS.SQZ")[:48]
    assert _sha1(frames) == GOLD_TITUS_IN


def test_present_fade_in_byte_exact():
    frames = fade_in_frames(_palette6("PRESENT.SQZ"), 0xFF)
    assert len(frames) == 256
    assert frames[0] == _BLACK
    assert _sha1(frames) == GOLD_PRESENT_IN


def test_titus_fade_out_byte_exact():
    # the fade-OUT starts from the held image palette = the fade-IN's converged frame
    held = fade_in_frames(_palette6("TITUS.SQZ"), 0x10)[-1]
    frames = fade_out_frames(held, 0x10)
    assert len(frames) == 16
    assert frames[0] == held                        # first retrace shows the full held palette
    assert frames[-1] == _BLACK                     # ends fully black
    assert _sha1(frames) == GOLD_TITUS_OUT


def test_fade_out_saturates_and_terminates():
    # the first n=0x10 entries of a pure-white DAC (0x3F) saturate down to black; entries past n are
    # left untouched. 0x3F -> 0 by -4 needs 16 passes, +1 terminal all-black pass = 17 retrace frames.
    frames = fade_out_frames(b"\x3f" * 0x300, 0x10)
    assert frames[0] == b"\x3f" * 0x300
    assert frames[-1][:48] == _BLACK[:48]           # the 16 faded entries are black
    assert frames[-1][48:] == b"\x3f" * (0x300 - 48)  # entries >= 16 untouched by n=0x10
    assert all(0 <= b <= 0x3F for f in frames for b in f)


def test_fade_in_clamp_does_not_overshoot_target():
    # a target above the 0x3D clamp (0x3F) is approached but not raised past 0x3E (the cmp al,0x3d; ja skip)
    frames = fade_in_frames(b"\x3f" * 3 + b"\x00" * (0x300 - 3), 1)
    assert frames[0][:3] == b"\x00\x00\x00"
    assert max(frames[-1][0:3]) <= 0x3E


# --- 911D PRESENT palette morph -------------------------------------------------------------------

GOLD_PRESENT_MORPH = "585c55c05c712d72893575dffb2f7628106390af"   # verified == VM DAC (234 frames)


@pytest.mark.skipif(not ACE7_FIXTURE.exists() or not (ASSETS / "PRESENT.SQZ").exists(),
                    reason="ACE7 morph-target fixture or PRESENT.SQZ not present")
def test_present_palette_morph_byte_exact():
    held = fade_in_frames(_palette6("PRESENT.SQZ"), 0xFF)[-1]      # the faded-in start palette
    target = ACE7_FIXTURE.read_bytes()                            # DGROUP 0xACE7 morph target
    phase = held[(0xFF - 1) * 3 + 1]                              # the BL the fade-in leaves (G of entry 0xFE)
    frames = palette_morph_frames(held, target, initial_phase=phase)
    assert len(frames) == 234
    assert _sha1(frames) == GOLD_PRESENT_MORPH


def test_palette_morph_converges_via_odd_staircase():
    # the loop stays alive only while SOME component snaps (reaches distance 1) each pass: a lone odd-distance
    # component ramps once then the loop breaks. A staircase of odd distances (1, 3, 5) snaps one per pass and
    # converges. comps 0/1/2 start at distance 1/3/5 below the (uniform) target.
    target = bytes([10] * 0x300)
    start = bytes([9, 7, 5] + [10] * (0x300 - 3))
    frames = palette_morph_frames(start, target, initial_phase=0)
    assert frames and frames[-1][:3] == b"\x0a\x0a\x0a"          # all three components reached the target


def test_palette_morph_no_change_holds_steady():
    # start already at target -> no ramps/snaps; the single pass still emits its retrace frames, all unchanged
    pal = bytes(range(0x40)) * 12                                 # 768 bytes, arbitrary 6-bit
    frames = palette_morph_frames(pal, pal, initial_phase=0)
    assert all(f == pal for f in frames)
