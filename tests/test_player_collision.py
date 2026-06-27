"""Tests for the recovered player collision leaves (pre2.recovered.player_collision).

ASM equivalence proven on live gameplay demos: collision_fall 791/791 (L1) + 138/138 (L6),
collision_hblock 98/98 (L1), collision_slope_offset 16/16 (slope demo 001513) + 8/8 (102854) -- all VERIFIED
byte-exact (the slope demo is a level with sloped/slippery tiles)."""
from __future__ import annotations

import pytest

from pre2.recovered.player_collision import (
    collision_airborne,
    collision_bridge_dip,
    collision_ceiling,
    collision_fall,
    collision_ground_handler,
    collision_hblock,
    collision_land,
    collision_side_handler,
    collision_slope_offset,
)


def test_fall_sets_airborne_bit():
    assert collision_fall(0) == 1
    assert collision_fall(2) == 3          # | 1
    assert collision_fall(1) == 1          # already set


def test_hblock_undoes_x_step_and_stops():
    assert collision_hblock(0x200, 0x40) == (0x1FC, 0)               # -sar(+0x40,4) = -4
    assert collision_hblock(0x200, (-0x40) & 0xFFFF) == (0x204, 0)   # -sar(-0x40,4) = +4
    assert collision_hblock(0x200, 0x0F) == (0x200, 0)               # sar(0x0F,4)=0


def test_slope_offset_disasm_formula():
    # non-slope tile (prop & 0x30 == 0) -> returned unchanged
    assert collision_slope_offset(0x05, 9) == 0x05
    # up-slope (0x10 set): quot=(X&0xF)//3 + (prop&0xF)
    assert collision_slope_offset(0x35, 9) == 3 + 5                  # quot 9//3=3, low 5
    # down-slope (0x10 clear): (prop&0xF) - quot
    assert collision_slope_offset(0x25, 9) == 5 - 3
    # down-slope with a negative result sign-extends to a word: quot=15//3=5, low=0 -> -5
    assert collision_slope_offset(0x20, 15) == (-5) & 0xFFFF


def test_land_rising_sets_airborne():
    # Yvel < 0 (still rising) -> just set the airborne flag, no Y snap
    mem = {0x4F2A: (-0x10) & 0xFFFF, 0x6BF3: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_land(rb, rw, lambda o: 0, 0)
    assert out[0x4F24] == 0 and out[0x6BF3] == 1
    assert 0x4F1E not in out


def test_land_soft_zeroes_yvel_and_sets_flags():
    # falling onto a flat (prop 0) tile with a small fall counter -> soft land
    mem = {0x4F2A: 0x20, 0x4F1E: 0x355, 0x4F1C: 0x100, 0x6BD2: 0, 0x6BE0: 5, 0x6BCA: 0x300}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_land(rb, rw, lambda o: 0, 0x200)   # read_es -> tile 0 -> prop table[0]=0 (flat)
    assert out[0x4F1E] == 0x350          # snapped to the tile top (& 0xFFF0)
    assert out[0x4F2A] == 0              # soft land zeroes Yvel
    assert out[0x6BF3] == 2 and out[0x6BD1] == 0
    assert out[0x6BE0] == 4              # [0x6BE0] saturating-decremented 5 -> 4
    assert out[0x6BCA] == 0x350


# --- ceiling (head-bump) collision 0x5C16 ---
# ASM equivalence proven byte-exact on the head-collision demos: collision_ceiling
# 247/247 (015602) + 53/53 (015822) + 97/97 (015934). Witnessed paths: tile-above
# handler idx 0 (noop) and idx 1 (head-bump, always Yvel<0 -> rising). The Yvel==0
# push-out (668B), the idx-2 trigger (0x65AF) and the 0x7E5E-solid side-nudge are
# unwitnessed across all demos and fail loud.

def _ceil_mem(handler_idx, player_tile_ceil_solid=0, yvel=(-0x10) & 0xFFFF, y=0x355):
    # tile-above maps to handler idx via 0x805E; player tile maps to ceiling-solid via 0x7E5E.
    ds = {0x4F2A: yvel & 0xFFFF, 0x4F1E: y, 0x4F1C: 0x100,
          (0x805E + 1): handler_idx, (0x7E5E + 2): player_tile_ceil_solid}
    rb = lambda o: ds.get(o, 0) & 0xFF
    rw = lambda o: ds.get(o, 0) & 0xFFFF
    read_es = lambda o: 1 if o == 0 else (2 if o == 0x100 else 0)  # tile_above=1, player_tile=2
    return rb, rw, read_es


def test_ceiling_noop_handler_does_nothing():
    rb, rw, read_es = _ceil_mem(handler_idx=0)
    assert collision_ceiling(rb, rw, read_es, 0) == {}


def test_ceiling_head_bump_zeroes_yvel_and_snaps_down():
    # idx 1, rising (Yvel<0): zero Yvel, snap Y to below the ceiling tile (&0xFFF0 + 0x10)
    rb, rw, read_es = _ceil_mem(handler_idx=1, yvel=(-0x20) & 0xFFFF, y=0x357)
    out = collision_ceiling(rb, rw, read_es, 0)
    assert out[0x4F2A] == 0
    assert out[0x4F1E] == 0x360          # (0x357 & 0xFFF0) + 0x10


def test_ceiling_head_bump_yvel0_fails_loud():
    # idx 1 with Yvel==0 takes the 668B push-out branch, which is unwitnessed
    rb, rw, read_es = _ceil_mem(handler_idx=1, yvel=0)
    with pytest.raises(NotImplementedError):
        collision_ceiling(rb, rw, read_es, 0)


def test_ceiling_trigger_handler_fails_loud():
    rb, rw, read_es = _ceil_mem(handler_idx=2)
    with pytest.raises(NotImplementedError):
        collision_ceiling(rb, rw, read_es, 0)


def test_ceiling_solid_side_nudge_slips_to_open_side():
    # idx 0 (no head-bump) but player tile is ceiling-solid and Y>0 -> corner-slip nudge. Xvel==0 -> dx=+1; the
    # neighbour tile (read_es != 0/0x100 -> 0, not ceiling-solid) is open, so X is nudged +2 toward it.
    rb, rw, read_es = _ceil_mem(handler_idx=0, player_tile_ceil_solid=1, y=0x100)
    out = collision_ceiling(rb, rw, read_es, 0)
    assert out[0x4F1C] == 0x102           # X (0x100) nudged +2 toward the open side


# --- ground tile-handler dispatch cs:[0x7D9B] (collision_ground_handler) ---
# ASM equivalence proven byte-exact: 3216 dispatches across 5 demos (015602/001513/102854/015934/015822),
# idx 0 (snap-or-fall, 65EF) + idx 1 (plain land, 6641) -- the only indices witnessed. idx 2-5 are thin
# compositions over the verified land core (ASM-matched); idx 6 (0x65AF trigger) fails loud.

def test_ground_idx1_is_plain_land():
    # idx 1 (6641) = collision_land directly; rising -> just airborne + slope-shift clear
    mem = {0x4F2A: (-0x10) & 0xFFFF, 0x6BF3: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_ground_handler(1, rb, rw, lambda o: 0, 0)
    assert out[0x4F24] == 0 and out[0x6BF3] == 1


def test_ground_idx0_snaps_down_to_reachable_tile():
    # idx 0 (65EF), at rest (Yvel 0), a flat solid tile one row below within reach -> step down + land
    mem = {0x4F2A: 0, 0x4F1E: 0x100, 0x4F1C: 0x80, 0x6BD2: 0, 0x6BE0: 0, 0x6BCA: 0x100}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    # read_es(di+0x100) -> tile 1; prop table[1] = 0x01 (flat, reachable, offset 1 < 0x10)
    read_es = lambda o: 1 if o == 0x100 else 0
    rb = lambda o: 0x01 if o == (0x8E1D + 1) else (mem.get(o, 0) & 0xFF)
    out = collision_ground_handler(0, rb, rw, read_es, 0)
    # Y steps down a row (0x100 + 0x10 = 0x110), then the land core adds the foot tile's slope offset (+1)
    assert out[0x4F1E] == 0x111


def test_ground_idx0_falls_when_nothing_below():
    # idx 0, at rest but the tile below is empty (prop 0) -> mark airborne
    mem = {0x4F2A: 0, 0x6BF3: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_ground_handler(0, rb, rw, lambda o: 0, 0)
    assert out == {0x6BF3: 1}


def test_ground_idx2_land_plus_slope_shift():
    mem = {0x4F2A: (-0x10) & 0xFFFF, 0x6BF3: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_ground_handler(2, rb, rw, lambda o: 0, 0)
    assert out[0x4F24] == 1                # 6657 sets the slope-shift byte to 1


def test_ground_idx7_noop_and_idx6_is_offcamera_trigger():
    rb = lambda o: 0
    rw = lambda o: 0
    assert collision_ground_handler(7, rb, rw, lambda o: 0, 0) == {}      # idx7 6672 = ret
    # idx6 65AF = off-camera trigger; with no lives ([0x27D8]==0) and not already armed -> game over
    assert collision_ground_handler(6, rb, rw, lambda o: 0, 0) == {0x6BE5: 1}


# --- bridge / platform sag-under-weight 0x5BB8 (collision_bridge_dip) ---
# ASM equivalence proven byte-exact: 3011 calls across 001513/015602/102854 (7 real dip/spring events on the
# slope demo, tiles 0xDE-0xE1). Springs the previous tile back up, dips the new bridge tile down, dirties the grid.

def _bridge_mem(bab=0x55AA, bridge_tiles=()):
    # 0x805E[id] bit 0x20 set => bridge frame; 0x4DF8[id] = 1 => grid-dirty path (not the 653D redraw)
    ds = {0x6BAB: bab}
    rb = lambda o: (0x20 if (o - 0x805E) in bridge_tiles else 0) if 0x805E <= o < 0x805E + 0x100 else \
                   (1 if 0x4DF8 <= o < 0x4DF8 + 0x100 else (ds.get(o, 0) & 0xFF))
    rw = lambda o: ds.get(o, 0) & 0xFFFF
    return ds, rb, rw


def test_bridge_no_dip_when_not_a_bridge_tile():
    ds, rb, rw = _bridge_mem(bab=0x55AA, bridge_tiles=())
    read_es = lambda o: 0x10            # plain tile, no 0x20 bit
    ds_w, map_w = collision_bridge_dip(0x200, read_es, rw, rb)
    assert ds_w == {} and map_w == {}


def test_bridge_dips_new_tile_down():
    # foot tile 0x20 is a bridge frame -> dip down to 0x21, mark dipping, dirty
    ds, rb, rw = _bridge_mem(bab=0x55AA, bridge_tiles={0x20, 0x21})
    read_es = lambda o: 0x20
    ds_w, map_w = collision_bridge_dip(0x300, read_es, rw, rb)
    assert map_w == {0x300: 0x21}                       # es:[di] = id + 1
    assert ds_w[0x6BAB] == 0x300                        # now the dipping tile
    assert ds_w[0x2DF4] == 1 and ds_w[0x2DE0] == 0x55AA  # grid dirtied


def test_bridge_springs_previous_tile_back_up():
    # a tile is already dipping at 0x280 (graphic 0x22); foot tile is a plain tile -> spring 0x280 back up
    ds, rb, rw = _bridge_mem(bab=0x280, bridge_tiles={0x21})  # 0x21 is still a sag frame, 0x20 is not
    es = {0x280: 0x22, 0x300: 0x10}
    read_es = lambda o: es[o]
    ds_w, map_w = collision_bridge_dip(0x300, read_es, rw, rb)
    # springback: 0x22-1=0x21 is a sag frame -> write 0x21; 0x21-1=0x20 not a sag frame -> stop, clear
    assert map_w == {0x280: 0x21}
    assert ds_w[0x6BAB] == 0x55AA


# --- horizontal/body side-collision dispatch cs:[0x7D95] (collision_side_handler) ---
# ASM equivalence proven byte-exact: 3208 dispatches across 4 demos. idx 0 (652C, the non-solid no-op path) +
# idx 1 (6539 = collision_hblock) are witnessed; the side-solid wall-marker push (64FA) never fires (ASM-matched).

def test_side_idx0_noop_when_not_side_solid():
    rb = lambda o: 0                       # 0x805E[tile] bit 0x10 clear
    rw = lambda o: 0
    assert collision_side_handler(0, lambda o: 0x10, rw, rb, 0x100) == {}


def test_side_idx1_is_horizontal_block():
    mem = {0x4F1C: 0x200, 0x4F22: 0x40}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_side_handler(1, lambda o: 0, rw, rb, 0)
    assert out == {0x4F1C: 0x1FC, 0x4F22: 0}   # collision_hblock: X -= sar(Xvel,4), Xvel=0


def test_side_idx0_wall_marker_pushed_when_side_solid():
    # 0x805E[tile] bit 0x10 set -> push (X<<3, Y<<3) into the first free 0x6EA9 slot
    mem = {0x4F1C: 0x100, 0x4F1E: 0x80, 0x6EA9: 0x55AA}
    rb = lambda o: (0x10 if o == (0x805E + 7) else (mem.get(o, 0) & 0xFF))
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_side_handler(0, lambda o: 7, rw, rb, 0)
    assert out[0x6EA9] == 0x800 and out[0x6EAB] == 0x400   # X<<3, Y<<3
    assert out[0x6EAD] == 0 and out[0x6EAE] == 0 and out[0x6EB0] == 0


def test_side_idx2_trigger_fails_loud():
    with pytest.raises(NotImplementedError):
        collision_side_handler(2, lambda o: 0, lambda o: 0, lambda o: 0, 0)


# --- airborne physics 0x63B5 (collision_airborne) ---
# ASM equivalence proven byte-exact: 1444 calls across 4 demos (gravity + air drift + fall anim).

def test_airborne_applies_gravity_and_clamps_terminal():
    # Yvel below terminal: += 0x10. [0x6BC5]=0, [0x6BDB]=0, [0x6BC7]=0.
    mem = {0x4F2A: 0x20, 0x4F22: 0, 0x4F25: 0x01, 0x6BD0: 0, 0x6BD2: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_airborne(rw, rb)
    assert out[0x4F2A] == 0x30                  # 0x20 + gravity 0x10
    assert out[0x6BE0] == 6                     # descending -> fall-dust counter armed
    assert out[0x4F20] == 0x000C                # fall anim frame 0xC (fall counter < 0xC)


def test_airborne_gravity_clamped_to_terminal():
    mem = {0x4F2A: 0xB8, 0x4F22: 0, 0x6BD0: 0, 0x6BD2: 0, 0x4F25: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_airborne(rw, rb)
    assert out[0x4F2A] == 0xC0                  # 0xB8 + 0x10 = 0xC8 clamped to terminal 0xC0


def test_airborne_rising_no_anim_change():
    # Yvel < 0 (rising) with [0x6BC5]=0 -> no anim / fall-dust writes
    mem = {0x4F2A: (-0x40) & 0xFFFF, 0x4F22: 0}
    rb = lambda o: mem.get(o, 0) & 0xFF
    rw = lambda o: mem.get(o, 0) & 0xFFFF
    out = collision_airborne(rw, rb)
    assert 0x4F20 not in out and 0x6BE0 not in out
