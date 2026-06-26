"""Tests for the secondary-entity render-injection keystone (pre2.recovered.object_inject).

Byte-exact ASM equivalence is proven live by the snapshot shadow (project_entity 480/480 on snapshot 154531);
these pin the allocator + the projection record/cull/mode contract."""
from __future__ import annotations

from pre2.recovered.object_inject import (INJECT_MODE, ProjectResult, OBJ_COUNT,
                                          find_free_object_slot, project_entity)


def test_find_free_first_empty_slot():
    ids = [0x140, 0x141, 0xFFFF, 0x142]          # slot 2 is free
    assert find_free_object_slot(lambda s: ids[s] if s < len(ids) else 0xFFFF) == 2


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
