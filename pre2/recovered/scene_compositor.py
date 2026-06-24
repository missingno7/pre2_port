"""SceneCompositor — layer a discrete screen as background + recovered dynamic overlays + present.

A non-gameplay screen (game-over, tally, menu, map) is composed of a (usually STATIC) full-screen
background plus recovered dynamic overlays drawn on top (object sprites, HUD, text). Some backgrounds
are loaded images whose decode/blit source is NOT yet recovered. Per the project discipline the faithful
renderer must NEVER silently copy the VM framebuffer to fake a complete frame — so an unrecovered
background is represented by an EXPLICIT gap layer, and the compositor still composes the recovered
overlays on top of it.

Background layers:
  * ``MissingBackgroundGap`` — the background's image source is not recovered yet. The compositor fills
    the background with a recognizable diagnostic marker (a coarse hatch) so it reads as "unresolved",
    NOT as the real art and NOT as the VM frame. Recovering the image is a tracked open task.
  * ``FixtureBackground`` — an ORACLE-CAPTURED background, allowed ONLY as a diagnostic fixture (probes/
    tests) to verify the recovered overlays compose correctly on top of a known plate. Never shipped as
    a final asset, never used by the live viewer.
  * ``RecoveredBackground`` — a genuinely recovered image (decode/blit). The end state.

``compose_scene`` returns the composited planes and a :class:`SceneStatus` so callers can tell a complete
frame from one with an unresolved background (the viewer shows the gap; verification asserts Δ=0 only for
COMPLETE or over a FIXTURE).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, List, Sequence, Tuple, Union

#: an overlay is a callable that draws onto (planes, page) in place — e.g. the object pass or the HUD.
Overlay = Callable[[Sequence[bytearray], int], None]

_PLANE_LEN = 0x10000
_ROW = 0x28


@dataclass(frozen=True)
class MissingBackgroundGap:
    """The background image is not recovered yet (tracked open task)."""
    name: str


@dataclass(frozen=True)
class FixtureBackground:
    """An oracle-captured background — DIAGNOSTIC FIXTURE ONLY (probes/tests), never shipped/live."""
    planes: Tuple[bytes, bytes, bytes, bytes]
    name: str


@dataclass(frozen=True)
class RecoveredBackground:
    """A genuinely recovered background image (decode/blit) — the end state."""
    planes: Tuple[bytes, bytes, bytes, bytes]


SceneBackground = Union[MissingBackgroundGap, FixtureBackground, RecoveredBackground]


class SceneStatus(Enum):
    COMPLETE = auto()         # recovered background + recovered overlays = a real faithful frame
    BACKGROUND_GAP = auto()   # background unrecovered: overlays are real, background is the marker
    FIXTURE = auto()          # background is an oracle fixture (diagnostics only)


def _fill_gap(planes: Sequence[bytearray], page: int) -> None:
    """Fill the viewport region with a recognizable 'unrecovered background' hatch (color 5 on 0).

    A coarse 8x8 diagonal hatch so it is unmistakably a placeholder, not real art and not the VM frame.
    Only the 200-row screen window from ``page`` is marked; the overlays draw on top.
    """
    for row in range(200):
        # diagonal stripe every 8 px: set the byte where (row + col) % 16 < 8 in plane 0 and 2 (color 5)
        for col in range(_ROW):
            off = (page + row * _ROW + col) & 0xFFFF
            bits = 0xAA if ((row >> 3) & 1) == 0 else 0x55
            if ((row + col) & 1) == 0:
                planes[0][off] = bits
                planes[2][off] = bits


def compose_scene(background: SceneBackground, overlays: List[Overlay], page: int):
    """Compose a scene into fresh planes; returns ``(planes, status)``.

    The overlays (recovered object pass / HUD / text) are always applied. The background layer determines
    the base pixels and the status. A ``FixtureBackground`` is accepted (for diagnostics) but the caller
    is responsible for only using it in probes/tests.
    """
    planes = [bytearray(_PLANE_LEN) for _ in range(4)]
    if isinstance(background, MissingBackgroundGap):
        _fill_gap(planes, page)
        status = SceneStatus.BACKGROUND_GAP
    elif isinstance(background, FixtureBackground):
        for p in range(4):
            planes[p][:len(background.planes[p])] = background.planes[p]
        status = SceneStatus.FIXTURE
    elif isinstance(background, RecoveredBackground):
        for p in range(4):
            planes[p][:len(background.planes[p])] = background.planes[p]
        status = SceneStatus.COMPLETE
    else:
        raise TypeError(f"unknown SceneBackground: {background!r}")
    for overlay in overlays:
        overlay(planes, page)
    return planes, status
