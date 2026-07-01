"""Front-end DAC palette fades (1030:919F fade-IN, 1030:9286 fade-OUT) — the title/scene fade primitive.

The non-gameplay 13h screens (TITUS / PREHISTORIK-2 title) fade their 256-colour DAC in from black and
out to black around the held image. This is a SEPARATE fade from the gameplay 16-colour fade
([[pre2 transition.py 6772]]): it ramps the **VGA DAC directly** (read-modify-write via ports 3C7/3C8/3C9)
one entry at a time, waiting a VGA retrace (44CD) every 32 entries, repeating full passes until the ramp
completes. Recovered as the exact per-retrace-frame DAC sequence the screen displays, so the native 13h
renderer applies the same palette each frame (no DAC ports).

ASM model (capstone on the unpacked 1030 segment):

  fade-IN  (919F loop 91CF-925C): ``cs:[9153]`` = entry count N (cl arg: TITUS 0x10, PRESENT 0xFF);
    ``cs:[9154]`` = entry index, 0..N-1. Per pass over indices 0..N-1:
      * when ``idx & 0x1F == 0`` -> ``call 44CD`` (one retrace; the DAC as it stands is on screen);
      * read DAC[idx] R,G,B (``in 3C9`` ×3, each ``& 0x3F``); for each component ``cur``:
        if ``target[idx*3+c] > cur`` AND ``cur <= 0x3D``  ->  ``cur += 2``  and mark "changed" (bp);
        write it back (``out 3C9``).
    After a full pass, if anything changed -> another pass; else done. Starts from a black DAC
    (91AD cleared it before the image copy).

  fade-OUT (9286 loop 92A1-9327): same N (``cs:[9153]`` left from the matching fade-IN), same retrace
    cadence. Per component: ``max(cur - 4, 0)`` (the ``sub al,1; adc al,0`` ×4 saturating decrement);
    mark changed while any component still > 0. Repeat passes until the whole DAC is black.

A "frame" here is one 44CD retrace wait (70 Hz). ``fade_frames`` returns the list of DAC snapshots, one
per retrace, that the screen shows during the fade — the byte-exact display sequence.
"""
from __future__ import annotations

_STEP_IN = 2          # [asm 921E] add al,2
_STEP_OUT = 4         # [asm 92D7-92E5] four saturating sub-1
_IN_CLAMP = 0x3D      # [asm 921A] cmp al,0x3d; ja skip  (don't raise a component already past 0x3d)


def _retrace_at(idx: int) -> bool:
    """[asm 91D9 / 92A3] ``test cs:[9154],0x1f; jne`` — wait a retrace whenever the index is a multiple of 32."""
    return (idx & 0x1F) == 0


def fade_in_frames(target: bytes, n_entries: int) -> list[bytes]:
    """The DAC sequence (one 0x300-byte snapshot per retrace) for a fade-IN from black to ``target``.

    ``target`` is the 768-byte (256×3) 6-bit destination palette (the asset's palette); ``n_entries`` is the
    DAC entry count the routine ramps (``cs:[9153]``). The displayed DAC starts black and rises ``+2``/pass."""
    dac = bytearray(0x300)
    frames: list[bytes] = []
    while True:
        changed = 0
        for idx in range(n_entries):
            if _retrace_at(idx):
                frames.append(bytes(dac))                      # [asm 91E1] retrace: this DAC is on screen
            base = idx * 3
            for c in range(3):
                cur = dac[base + c]
                if target[base + c] > cur and cur <= _IN_CLAMP:   # [asm 9216/921A] jbe / ja skip
                    dac[base + c] = cur + _STEP_IN                 # [asm 921E] add al,2
                    changed += 1                                   # [asm 9220] inc bp
        if changed == 0:                                          # [asm 9255] or bp,bp; je done
            break
    return frames


def fade_out_frames(start: bytes, n_entries: int) -> list[bytes]:
    """The DAC sequence (one snapshot per retrace) for a fade-OUT from ``start`` to black.

    ``start`` is the 768-byte 6-bit palette currently displayed (the held image's palette); each pass
    lowers every component by ``max(cur-4, 0)`` until the whole DAC is black."""
    dac = bytearray(start[:0x300])
    if len(dac) < 0x300:
        dac.extend(b"\x00" * (0x300 - len(dac)))
    frames: list[bytes] = []
    while True:
        changed = 0
        for idx in range(n_entries):
            if _retrace_at(idx):
                frames.append(bytes(dac))                      # [asm 92AB] retrace
            base = idx * 3
            for c in range(3):
                cur = dac[base + c]
                new = cur - _STEP_OUT if cur > _STEP_OUT else 0   # [asm 92D5-92E9] saturating -4
                dac[base + c] = new
                changed |= cur                                    # [asm 92E7] or bp,ax — ax=ah:al with
                #   ah=the ORIGINAL component (pre-decrement), so the loop runs one more pass after a
                #   component first hits 0 (it stops only on a pass that STARTS all-black). cur>=new, so
                #   `|= cur` is equivalent to `or bp,(cur<<8)|new` for the non-zero test.
        if changed == 0:                                          # [asm 9327] or bp,bp; je ret
            break
    return frames


def fade_frames(palette: bytes, n_entries: int, *, direction: str) -> list[bytes]:
    """Convenience: ``direction='in'`` -> :func:`fade_in_frames`, ``'out'`` -> :func:`fade_out_frames`."""
    if direction == "in":
        return fade_in_frames(palette, n_entries)
    if direction == "out":
        return fade_out_frames(palette, n_entries)
    raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")


_MORPH_STRIDE = 0x52      # [asm 9115] cmp bl,0x52 — a retrace every 82 component writes
_MORPH_STEP = 2           # [asm 90FD] ah=2 — ramp ±2 per pass


def palette_morph_frames(start: bytes, target: bytes, *, initial_phase: int = 0) -> list[bytes]:
    """[asm 9090 / 911D loop] The PREHISTORIK-2 (PRESENT.SQZ) title's palette MORPH — the held-image colours
    drift toward a second palette (``DS:0xACE7``) over a sustained animation, the title's "reveal".

    Per pass over all 256 DAC entries (768 components): each component ramps ``±2`` toward ``target`` (snapping
    when within 1 — that snap is what keeps the loop alive: ``bp`` counts snaps and the loop repeats while any
    occurred). A retrace wait fires after every ``0x52`` (82) component WRITES, and that counter is **continuous
    across passes** — it is only reset right after a retrace, never at a pass boundary. ``initial_phase`` is the
    leftover of that counter on entry (the ``BL`` register the preceding fade-in leaves = the green of its last
    entry), which sets where the first retrace lands.

    Returns the DISPLAYED DAC sequence, one 0x300-byte 6-bit snapshot per retrace. The displayed palette commits
    **per VGA DAC entry** (every 3 ``out 3C9``): a retrace that lands mid-entry still shows that entry's previous
    committed value, so the snapshot is NOT the raw per-component working buffer."""
    saved = bytearray(start[:0x300])                      # the working buffer (read each pass)
    displayed = bytearray(start[:0x300])                  # the committed DAC = what is on screen
    frames: list[bytes] = []
    bl = initial_phase & 0xFF
    while True:
        changed = 0                                       # [asm bp] components that snapped this pass
        latch = 0                                         # components latched toward the current DAC entry
        for i in range(0x300):
            cur = saved[i]
            diff = (target[i] - cur) & 0xFF
            adiff = diff if diff < 0x80 else 256 - diff   # |signed diff|  [asm 90F5-90FB]
            if diff == 0:                                 # [asm 90F7] je: already at target
                pass
            elif adiff < _MORPH_STEP:                     # [asm 9101] |diff|==1: snap to target
                changed += 1
                saved[i] = target[i]
            else:                                         # [asm 9108-910E] ramp ±2 toward target
                saved[i] = (cur + _MORPH_STEP) if target[i] > cur else (cur - _MORPH_STEP)
            latch += 1                                    # [asm 9112] out 3C9 (write idx resets to 0 each pass)
            if latch == 3:                                # a full DAC entry committed -> now displayed
                displayed[i - 2:i + 1] = saved[i - 2:i + 1]
                latch = 0
            bl += 1                                       # [asm 9113] inc bl
            if bl == _MORPH_STRIDE:                       # [asm 9115-911D] retrace, then bl=0
                frames.append(bytes(displayed))
                bl = 0
        if changed == 0:                                  # [asm 9121] or bp,bp; je ret
            break
    return frames
