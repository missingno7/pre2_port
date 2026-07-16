"""Gate: every byte the demo-record tail can write is routed to the object graph.

Found by PLAYING the game, not by the corpus. After the Stage 2.5 boot-flip (a25acc1) made the object graph the
product default, a real session died on::

    AssertionError: gameplay tick wrote to the read-only loaded data at 0x006D
                    — an un-routed mutable byte (the object graph is not the complete store)

Root cause: ``input_decode.decode_input``'s demo RECORD tail writes at ``DS:[DEMO_PTR + 0x3F]`` with the cursor
running to ``RECORD_LIMIT`` (0x7FC), so ANY byte in 0x003F..0x083C is writable at runtime. ``graph_layout``'s
``_BUFFERS`` carve that span into named pieces, and it had two HOLES — 0x006D..0x00D5 and 0x0125..0x02E9, 558
bytes. No recorded demo runs long enough to walk the cursor into them, so every corpus proof passed while a
real playthrough crashed within seconds.

This pins the span so the next hole fails here instead of under a player.
"""


def _covered() -> set:
    import pre2.native.graph_layout as G
    covered = set()
    for _attr, base, ln in G._BUFFERS:
        covered.update(range(base, base + ln))
    for _attr, offs in G._SPARSE:
        covered.update(offs)
    for _attr, _cls, layout, base, count, stride in G._ROUTES:
        for k in range(count):
            b0 = base + k * stride
            for _f, off, w, _s in layout:
                covered.update(range(b0 + off, b0 + off + w))
    covered.update(range(G._ARENA_LO, G._ARENA_HI + 1))
    return covered


def test_the_whole_demo_record_span_is_routed_to_the_object_graph():
    from pre2.recovered.input_decode import RECORD_LIMIT
    lo = 0x3F                      # decode_input writes at DS:[DEMO_PTR + 0x3F]
    hi = 0x3F + RECORD_LIMIT + 2   # ..through the cursor limit, +2 for the 2-byte entry
    missing = sorted(o for o in range(lo, hi) if o not in _covered())
    assert not missing, (
        f"{len(missing)} byte(s) in the demo-record span 0x{lo:04X}..0x{hi - 1:04X} are NOT routed to the "
        f"object graph, starting at 0x{missing[0]:04X}. decode_input can write any of them at runtime, so "
        f"under readonly_image=True this is a player crash. Add them to graph_layout._BUFFERS.")


def test_routed_buffers_do_not_overlap():
    """Two buffers claiming one offset silently last-wins in the backend's map, so one of them would be
    unreachable — a wrong-state bug that no corpus proof would surface."""
    import pre2.native.graph_layout as G
    owner, dupes = {}, []
    for attr, base, ln in G._BUFFERS:
        for o in range(base, base + ln):
            if o in owner:
                dupes.append(f"0x{o:04X}: {owner[o]} vs {attr}")
            owner[o] = attr
    assert not dupes, f"overlapping _BUFFERS entries: {dupes[:8]}"


def test_strict_object_graph_survives_random_play_from_a_COLD_BOOT():
    """The regression net that would actually have caught 0x006D — and the reason none of the others did.

    Every recorded demo seeds mid-session with ``DEMO_PTR`` ALREADY ADVANCED (the gorilla seed starts at
    0x0620, writing at 0x065F — past the hole). Only a COLD BOOT starts the cursor low (the crashing session
    had ``DEMO_PTR`` = 0x002E → 0x003F + 0x2E = 0x006D). So 5456 corpus ticks, the whole verify family and a
    random-input fuzz seeded FROM A DEMO were all green, while a real player died in seconds.

    Verified to be a real net, not theatre: with the routing hole restored this catches it at tick 24; a
    demo-seeded fuzz of 4200 ticks never does. Cold-boot seeding is the thing that matters — recorded state
    starts too far along to exercise the beginning of any cursor.
    """
    import random
    import pytest
    from pathlib import Path
    from pre2.gaps import Pre2HybridGap
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.game_tick_demo import _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.object_runtime import to_object_store
    from pre2.native.seams import KBD

    root = Path(__file__).resolve().parents[1]
    if not (root / "assets").exists():
        pytest.skip("game assets not present")
    rnd = random.Random(0)
    state = native_cold_boot(str(root / "assets"), level=0)
    to_object_store(state)          # STRICT: any un-routed mutable write raises
    for i in range(120):            # the 0x006D write lands at ~tick 24
        _inject(state, bytes(rnd.choice((0, 1)) for _ in KBD), rnd.randrange(0, 0x200))
        try:
            native_gameplay_frame(state)
        except Pre2HybridGap:
            break                   # an unrecovered path — not what this test is about
        except AssertionError as e:
            raise AssertionError(f"un-routed mutable write during cold-boot play at tick {i}: {e}") from e


def test_the_product_default_store_does_not_crash_on_an_unrouted_write():
    """The posture, pinned. ``readonly_image=True`` is a VERIFICATION invariant (it proves the graph is the
    complete store); the verify scripts keep it via ``ObjectStore()``'s own default. The PRODUCT must not use
    it: an un-routed write is a modelling gap, not unrecovered behaviour, and it lands losslessly in the
    residue image. Crashing a player to enforce an architectural aspiration is the wrong trade."""
    from pre2.native.object_runtime import ObjectStore
    from pre2.native.runtime import _default_store

    assert ObjectStore().readonly_image is True, (
        "ObjectStore's own default must stay strict — the verify scripts rely on it to prove completeness")
    assert _default_store().readonly_image is False, (
        "the PRODUCT's default store must not assert readonly_image: a modelling gap would crash the player "
        "(it did, at 0x006D) instead of falling back to the documented residue path")
