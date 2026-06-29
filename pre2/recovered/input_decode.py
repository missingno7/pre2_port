"""The per-frame input-source decoder — 1030:0DC1 (``decode_input``).

This is the front of the player update (called from the 5850 wrapper at 0x58A4): it produces the six input
flags ``[0x27E8..0x27ED]`` the player FSM reads, from one of three sources selected by ``[0x2879]``:

* **mode 0 — live**: clear the flags, read the joystick (0xD6B; absent on keyboard play -> skipped), then OR
  the keyboard scancode flags ([0x28xx], set by the INT 09 handler) into the six flags. Then the *record tail*
  RLE-appends the packed input to the demo buffer at ``DS:0x3F`` (advancing ``[0x287a]``) — so a live session
  is simultaneously recorded.
* **mode 1 — playback**: RLE-*decode* the next packed byte from the demo buffer (``[0x287a]`` ptr / ``[0x287c]``
  byte / ``[0x287d]`` repeat-count) and unpack it into the six flags; skip the keyboard read.
* **mode 2 — record**: decode head + live read + record tail (the dispatch's union of both; unused in practice).

The six flags are a 6-bit packing (bit0..5 -> ``[0x27ED,0x27EC,0x27EA,0x27EB,0x27E8,0x27E9]``); the record re-pack
is the exact inverse of the playback unpack, so a recorded demo replays identically.

Pure: a function of DGROUP only (``rb``/``rw``) returning a ``{offset: (value, width)}`` write contract — no
``cpu``/``mem``. The one hardware dependency (the joystick port 0x201 read, reached only when a joystick is
actually present) is fenced off as :class:`Pre2InputGap`; no keyboard/demo path reaches it. Verified byte-exact
against the ASM over the demos (see tests/test_input_decode.py + pre2/probes/probe_input_decode_shadow.py).

[asm origin] 1030:0DC1..0F7E, joystick gate 1030:0D6B.
"""
from __future__ import annotations

from pre2.islands import oracle_link

MODE = 0x2879        # input source: 0=live keyboard, 1=demo playback, 2=record
DEMO_PTR = 0x287A    # demo buffer cursor (word; entries are 2 bytes at DS:[ptr+0x3F])
DEMO_BYTE = 0x287C   # current packed input byte (playback)
DEMO_CNT = 0x287D    # remaining repeat count for DEMO_BYTE (playback)
DEMO_FLAG = 0x6BE5   # set on a pending demo flag ([0x2874]) or the 0x55AA end sentinel
DEMO_HDR_LEVEL = 0x83E   # demo header: level id ([0x2D8A]) stamped at ptr==0
LEVEL = 0x2D8A
RECORD_LIMIT = 0x7FC     # demo buffer is full once the cursor reaches this

# OUTPUT flags, in unpack order (bit0 first): [0x27ED,0x27EC,0x27EA,0x27EB,0x27E8,0x27E9].
_UNPACK_ORDER = (0x27ED, 0x27EC, 0x27EA, 0x27EB, 0x27E8, 0x27E9)
# RE-pack order (MSB first): [0x27E9,0x27E8,0x27EB,0x27EA,0x27EC,0x27ED] — the exact inverse of unpack.
_PACK_ORDER = (0x27E9, 0x27E8, 0x27EB, 0x27EA, 0x27EC, 0x27ED)

# Each output flag = OR of these keyboard scancode-flag bytes ([0x28xx], set by INT 09). [asm 0EA4..0F06]
_KBD_SOURCES = {
    0x27EA: (0x283C, 0x2870, 0x281D, 0x2804),
    0x27EB: (0x2844, 0x286F, 0x283E, 0x2812),
    0x27EC: (0x2841, 0x286D, 0x2842, 0x281B),
    0x27ED: (0x283F, 0x286E, 0x281F, 0x281A),
    0x27E8: (0x2810, 0x282D, 0x2868, 0x284B),
    0x27E9: (0x2840,),
}

JOYSTICK_CFG = 0x27E4    # bit7 set => joystick absent [asm 0D6C..0D72]
JOYSTICK_DISABLE = 0x27D9  # nonzero => joystick absent [asm 0D74..0D79]


class Pre2InputGap(Exception):
    """Raised for the joystick-present branch (a port 0x201 hardware read at 0xD6B). No keyboard or demo-replay
    path reaches it — the joystick is gated absent — so the recovered decoder fails loud rather than guessing a
    hardware input value."""


def _joystick_absent(rb) -> bool:
    """0xD6B's gate: a joystick is absent (carry set, no port read) iff cfg bit7 is set or the disable flag is
    nonzero. [asm 0D6C: mov cl,[27E4]; or cl,cl; js absent / 0D74: cmp [27D9],0; jne absent]"""
    return (rb(JOYSTICK_CFG) & 0x80) != 0 or rb(JOYSTICK_DISABLE) != 0


@oracle_link("1030:0DC1",
             "per-frame input-source decoder: dispatch on [0x2879] (0=live keyboard / 1=demo playback / "
             "2=record). Live: clear the six FSM input flags [0x27E8..0x27ED], skip the absent joystick (0xD6B "
             "gate: [0x27E4] bit7 or [0x27D9]!=0), OR the [0x28xx] scancode flags into them, then RLE-append "
             "the 6-bit packed input to the demo buffer at DS:[0x287a]+0x3F (cursor +2 / repeat-count bump; "
             "level header [0x83E] at cursor 0). Playback: RLE-decode the buffer ([0x287a]/[0x287c]/[0x287d]) "
             "and unpack into the flags; the 0x55AA sentinel sets [0x6BE5]. Pack/unpack are mutual inverses.",
             "VERIFIED", merge_target="player_update")
def decode_input(rb, rw):
    """Run DC1 over DGROUP; return the ``{offset: (value, width)}`` write contract (the six input flags plus,
    per mode, the demo cursor/record-buffer side effects). ``rb``/``rw`` read DGROUP (DS-relative)."""
    writes: dict[int, tuple[int, int]] = {}
    mode = rb(MODE)
    ptr = rw(DEMO_PTR)                       # local cursor; head + tail both update it (no read-after-write)

    # --- demo-decode head: modes 1 and 2 (mode 0 jumps past it) -------------- [asm 0DCF je 0xE51]
    if mode != 0:
        if rb(0x2874) != 0:                  # [asm 0DD6..0DDD]
            writes[DEMO_FLAG] = (1, 1)
        if ptr != 0 and rb(DEMO_CNT) != 0:   # reuse the current byte [asm 0DE6/0DEA jne 0xE0E]
            writes[DEMO_CNT] = ((rb(DEMO_CNT) - 1) & 0xFF, 1)   # [asm 0E0E dec [287D]]
            al = rb(DEMO_BYTE)                                  # [asm 0E12 al=[287C]]
        else:                                # read the next 2-byte entry [asm 0DF1]
            lo = rb((ptr + 0x3F) & 0xFFFF)
            hi = rb((ptr + 0x40) & 0xFFFF)
            ptr = (ptr + 2) & 0xFFFF
            if ((lo | (hi << 8)) & 0xFFFF) == 0x55AA:           # end-of-demo sentinel [asm 0DF7]
                writes[DEMO_FLAG] = (1, 1)
            writes[DEMO_PTR] = (ptr, 2)                         # [asm 0E01]
            writes[DEMO_BYTE] = (lo, 1)                         # [asm 0E05 al]
            writes[DEMO_CNT] = (hi, 1)                          # [asm 0E08 ah]
            al = lo
        for off in _UNPACK_ORDER:            # unpack 6 bits -> flags [asm 0E15..0E4D]
            writes[off] = (al & 1, 1)
            al >>= 1

    # --- mode 1 (playback): the decoded flags are the answer ----------------- [asm 0E51/0E58 jmp 0xF75]
    if mode == 1:
        return writes

    # --- live read: modes 0 and 2 ------------------------------------------- [asm 0E5B clears the flags]
    if not _joystick_absent(rb):             # [asm 0E6F call 0xD6B; jb skips the joystick block]
        raise Pre2InputGap("DC1 joystick present: port 0x201 read at 0xD6B (no keyboard/demo path reaches it)")
    out = {off: 0 for off in _UNPACK_ORDER}
    for off, sources in _KBD_SOURCES.items():            # OR the scancode flags [asm 0EA4..0F06]
        v = 0
        for src in sources:
            v |= rb(src)
        out[off] = v & 0xFF
        writes[off] = (out[off], 1)

    # --- record tail: append the packed input to the demo buffer (RLE) ------- [asm 0F0A]
    if ptr >= RECORD_LIMIT:                  # buffer full [asm 0F0E jae 0xF75]
        return writes
    al = 0
    for off in _PACK_ORDER:                  # re-pack the flags (inverse of unpack) [asm 0F14..0F44]
        al = ((al << 1) | (out[off] & 1)) & 0xFF
    if ptr == 0:                             # first entry: stamp the level header, then a new entry [asm 0F48]
        writes[DEMO_HDR_LEVEL] = (rb(LEVEL), 1)
        writes[(ptr + 0x3F) & 0xFFFF] = (al, 1)
        writes[(ptr + 0x40) & 0xFFFF] = (0, 1)
        writes[DEMO_PTR] = ((ptr + 2) & 0xFFFF, 2)
    else:
        prev = rb((ptr + 0x3D) & 0xFFFF)     # the previously-recorded byte ([+0x3F] of the prior entry)
        cnt = rb((ptr + 0x3E) & 0xFFFF)
        if prev == al and cnt != 0xFF:       # same input, room to count -> bump the repeat [asm 0F5D inc]
            writes[(ptr + 0x3E) & 0xFFFF] = ((cnt + 1) & 0xFF, 1)
        else:                                # changed or count maxed -> new entry [asm 0F67]
            writes[(ptr + 0x3F) & 0xFFFF] = (al, 1)
            writes[(ptr + 0x40) & 0xFFFF] = (0, 1)
            writes[DEMO_PTR] = ((ptr + 2) & 0xFFFF, 2)
    return writes
