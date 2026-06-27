"""Tests for the secondary-entity render-injection keystone (pre2.recovered.object_inject).

Byte-exact ASM equivalence is proven live by the snapshot shadow (project_entity 480/480 on snapshot 154531);
these pin the allocator + the projection record/cull/mode contract."""
from __future__ import annotations

from pre2.recovered.object_inject import (INJECT_MODE, ProjectResult, OBJ_COUNT, OBJ_BASE,
                                          find_free_object_slot, handler_player_trail,
                                          lookup_anim_frame, project_entity)


def test_find_free_first_empty_slot():
    ids = [0x140, 0x141, 0xFFFF, 0x142]          # slot 2 is free
    assert find_free_object_slot(lambda s: ids[s] if s < len(ids) else 0xFFFF) == 2


def test_lookup_anim_frame_scans_section_then_id():
    # table at 0xA86F: a 0x7D01 section for type 5, then id entries; id 0x200 -> target 0x200-0x138=0xC8
    table = {0xA871: 0x7D01, 0xA873: 0x0005, 0xA875: 0x0100, 0xA877: 0x00C8}
    rw = lambda o: table.get(o & 0xFFFF, 0)
    assert lookup_anim_frame(rw, 0x200, 5) == 0xA877


# --- handler_player_trail (7D9B) — shadow byte-exact vs ASM (witness demo 213332: gates + 1 full draw) ---
def test_player_trail_throttle_gate_updates_counter_no_draw():
    SI = 0x4000
    # not level 5; counter 0->1, throttle [si+6]=5 > (1>>2)=0 -> not drawn (but counter is written)
    kv = {0x2D8A: 8, SI + 7: 0, SI + 6: 5}
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    out, drawn = handler_player_trail(rb, rw, lambda o: 0, SI, lambda: None)
    assert drawn is False
    assert out == {SI + 7: (1, 1)}


def test_player_trail_full_draw_snaps_to_ground():
    SI = 0x4000
    kv = {0x2D8A: 8, SI + 7: 0, SI + 6: 0,               # not level 5; counter passes the throttle
          SI + 9: 0, SI + 0xA: 0, SI + 0xB: 0xFF, SI + 0xC: 0xFF,  # wide proximity window
          0x4F1C: 0x100, 0x4F1E: 0x100,                  # player at tile cell (0x10, 0x10)
          0x4FD4: 0xFFFF,                                 # object slot 0 is free
          0xA341: 0, 0xA343: 0,                          # ring index 0, offset[0] = 0
          0x2CF5: 0x20,                                   # map height 0x20 rows
          0x7F5E + 1: 1,                                  # terrain[tile 1] = solid (0 = empty)
          SI + 2: 0x1234, SI + 5: 0x40}                  # sprite id, flip byte
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    # a standable surface: solid tile (1) at bp=0x1410, empty (0) one + two above
    read_es = lambda o: 1 if (o & 0xFFFF) == 0x1410 else 0
    find_free = lambda: find_free_object_slot(lambda s: rw(0x4FD0 + s * 0x12 + 4))
    out, drawn = handler_player_trail(rb, rw, read_es, SI, find_free)
    assert drawn is True
    base = OBJ_BASE                                        # slot 0
    assert out[base + 0x00] == (0x100, 2)                 # X = playerX + offset
    assert out[base + 0x02] == (0x140, 2)                 # Y = ground row 0x14 * 16
    assert out[base + 0x04] == (0x1234, 2)                # sprite id
    assert out[SI + 4] == (0x17, 1)                       # entity mode
    assert out[0xA32E] == (base, 2)                       # projected-slot pointer
    assert out[0xA341] == (2, 2)                          # offset ring advanced


def test_lookup_anim_frame_skips_wrong_type_section():
    # a 0x7D01 section for the WRONG type (3) first, then the right one (5)
    table = {0xA871: 0x7D01, 0xA873: 0x0003, 0xA875: 0x00C8,        # type-3 section (skipped)
             0xA877: 0x7D01, 0xA879: 0x0005, 0xA87B: 0x00C8}         # type-5 section -> match
    rw = lambda o: table.get(o & 0xFFFF, 0)
    assert lookup_anim_frame(rw, 0x200, 5) == 0xA87B


def test_find_free_none_when_full():
    assert find_free_object_slot(lambda s: 0x100) is None      # all 12 taken


def test_find_free_scans_all_twelve():
    ids = [0x100] * (OBJ_COUNT - 1) + [0xFFFF]    # only the last slot is free
    assert find_free_object_slot(lambda s: ids[s]) == OBJ_COUNT - 1


# on-screen helper: px tile = px>>4; visible if (tile - cam) in [-2,22]x[-2,13]. cam at the entity tile -> 0.
_CAMX, _CAMY = 0x10, 0x08


def test_project_on_screen_builds_record():
    pr = project_entity(entry_x=0x100, entry_y=0x80, entry_sprite=0x172, entry_aux5=0x55,
                        entry_ptr=0x8489, cam_x=_CAMX, cam_y=_CAMY, find_free=lambda: 3)
    assert pr.drawn and pr.slot == 3 and pr.mode == INJECT_MODE
    assert pr.record[0x00] == 0x100 and pr.record[0x02] == 0x80     # X, Y
    assert pr.record[0x04] == 0x172 and pr.record[0x06] == 0x8489   # sprite id, back-pointer
    assert pr.record[0x08] == 0 and pr.record[0x0A] == 0            # velocity zeroed
    assert pr.record[0x0E] == 0 and pr.record[0x0F] == 0x55 and pr.record[0x10] == 0


def test_project_off_screen_not_drawn():
    pr = project_entity(entry_x=0x100, entry_y=0x80, entry_sprite=0x172, entry_aux5=0,
                        entry_ptr=0x8489, cam_x=0x80, cam_y=_CAMY, find_free=lambda: 3)   # far off-screen X
    assert pr == ProjectResult(False) and pr.record is None and pr.mode is None


def test_project_no_free_slot_not_drawn():
    pr = project_entity(entry_x=0x100, entry_y=0x80, entry_sprite=0x172, entry_aux5=0,
                        entry_ptr=0x8489, cam_x=_CAMX, cam_y=_CAMY, find_free=lambda: None)
    assert not pr.drawn and pr.record is None
