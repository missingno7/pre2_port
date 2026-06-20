"""Frame-accurate translation of physical key events into scan-code delivery.

Some DOS games poll their key-state table once per rendered frame.  A key
therefore has to be *held down for at least one full frame* to be observed.  A
quick tap can deliver its press and release between two frames; if both are
applied before the frame runs, the key is set and cleared before the game ever
polls it and the press is silently lost.

``KeyDispatcher`` sits between the UI (which posts raw key up/down events from any
thread) and the interpreter (which calls :meth:`pump` once per frame).  It
delivers a make code as soon as a key goes down and defers the matching break
until the key has been held for at least one frame, so every tap is seen.
"""
from __future__ import annotations

import collections
from typing import Callable


class KeyDispatcher:
    def __init__(self, deliver: Callable[[int], None]) -> None:
        # ``deliver`` is called with an XT scan code (make, or make|0x80 for break).
        self._deliver = deliver
        self._events: "collections.deque[tuple[str, int]]" = collections.deque()
        self._down: dict[int, int] = {}   # scancode -> frames held so far
        self._release: set[int] = set()   # scancodes with a release pending

    # Posted from the UI thread; deque ops are atomic under the GIL.
    def post_down(self, scancode: int) -> None:
        self._events.append(("down", scancode & 0xFF))

    def post_up(self, scancode: int) -> None:
        self._events.append(("up", scancode & 0xFF))

    def _drain_events(self) -> None:
        while self._events:
            kind, sc = self._events.popleft()
            if kind == "down":
                self._release.discard(sc)      # a re-press cancels a pending release
                if sc not in self._down:
                    self._deliver(sc)          # make code
                    self._down[sc] = 0
            else:
                self._release.add(sc)

    def _release_ready(self, *, hold_new_taps: bool) -> None:
        for sc in list(self._release):
            if self._down.get(sc, -1) >= 1 or not hold_new_taps:
                self._deliver(sc | 0x80)       # break code
                self._down.pop(sc, None)
                self._release.discard(sc)

    def pump_events(self) -> None:
        """Apply queued physical events without advancing the game-frame age.

        The interactive runner uses this during long no-frame loading bursts so
        a key released by the user does not remain logically held until the next
        visible frame.  New down+up taps drained here are released immediately;
        frame-start ``pump()`` remains the path that guarantees a tap spans one
        complete game poll.
        """
        self._drain_events()
        self._release_ready(hold_new_taps=False)

    def pump(self, *, allow_release: bool = True) -> None:
        """Apply queued events for one emulated boundary.

        ``allow_release=False`` is used by the interactive player immediately
        after a visual presenter boundary.  A legacy gameplay loop can present
        the frame before it checks some one-shot keys such as Esc, so releasing
        a quick tap at that boundary would clear the game's key table before
        the original post-present input code can observe it.  We still drain
        new key-down events and age held keys; the matching break is simply kept
        pending until a later timer/no-frame boundary.
        """
        self._drain_events()
        # Only release keys that have already been held for a full frame, and
        # only at boundaries where the caller knows post-present input polling is
        # not still ahead of the VM.
        if allow_release:
            self._release_ready(hold_new_taps=True)
        for sc in self._down:
            self._down[sc] += 1
