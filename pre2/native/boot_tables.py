"""Authoritative readable constants for the native cold-start — the SOURCE OF TRUTH for the boot data.

These were the fossilised tables inside the DOS DATA segment (pre2/native/boot_data.py's opaque blob). They now
live here as real Python values; the shipped game cold-starts from THESE. The DOS byte layout is GENERATED from
them by the detachable bridge (pre2/bridge/boot_layout.py) purely to verify byte-exact against the DOS original
-- ship without the bridge and the original memory layout is gone, only these constants remain.

Generated once by extracting the boot image; edit the values here, not the blob.
"""
from __future__ import annotations

# [0x6F90] the 256-entry signed sine table (amplitude +-64) -- the DOS build's own fixed-point generator
SINE_TABLE = [
    0, 1, 3, 4, 6, 7, 9, 10, 12, 14, 15, 17, 18, 20, 21, 23,
    24, 25, 27, 28, 30, 31, 32, 34, 35, 36, 38, 39, 40, 41, 42, 44,
    45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 54, 55, 56, 57, 57, 58,
    59, 59, 60, 60, 61, 61, 62, 62, 62, 63, 63, 63, 63, 63, 63, 63,
    64, 63, 63, 63, 63, 63, 63, 63, 62, 62, 62, 61, 61, 60, 60, 59,
    59, 58, 57, 57, 56, 55, 54, 54, 53, 52, 51, 50, 49, 48, 47, 46,
    45, 44, 42, 41, 40, 39, 38, 36, 35, 34, 32, 31, 30, 28, 27, 25,
    24, 23, 21, 20, 18, 17, 15, 14, 12, 10, 9, 7, 6, 4, 3, 1,
    -1, -2, -4, -5, -7, -8, -10, -11, -13, -15, -16, -18, -19, -21, -22, -24,
    -25, -26, -28, -29, -31, -32, -33, -35, -36, -37, -39, -40, -41, -42, -43, -45,
    -46, -47, -48, -49, -50, -51, -52, -53, -54, -55, -55, -56, -57, -58, -58, -59,
    -60, -60, -61, -61, -62, -62, -63, -63, -63, -64, -64, -64, -64, -64, -64, -64,
    -64, -64, -64, -64, -64, -64, -64, -64, -63, -63, -63, -62, -62, -61, -61, -60,
    -60, -59, -58, -58, -57, -56, -55, -55, -54, -53, -52, -51, -50, -49, -48, -47,
    -46, -45, -43, -42, -41, -40, -39, -37, -36, -35, -33, -32, -31, -29, -28, -26,
    -25, -24, -22, -21, -19, -18, -16, -15, -13, -11, -10, -8, -7, -5, -4, -2,
]
# [0x7090] its quarter-phase cosine companion
COSINE_TABLE = [
    64, 63, 63, 63, 63, 63, 63, 63, 62, 62, 62, 61, 61, 60, 60, 59,
    59, 58, 57, 57, 56, 55, 54, 54, 53, 52, 51, 50, 49, 48, 47, 46,
    45, 44, 42, 41, 40, 39, 38, 36, 35, 34, 32, 31, 30, 28, 27, 25,
    24, 23, 21, 20, 18, 17, 15, 14, 12, 10, 9, 7, 6, 4, 3, 1,
    0, -2, -4, -5, -7, -8, -10, -11, -13, -15, -16, -18, -19, -21, -22, -24,
    -25, -26, -28, -29, -31, -32, -33, -35, -36, -37, -39, -40, -41, -42, -43, -45,
    -46, -47, -48, -49, -50, -51, -52, -53, -54, -55, -55, -56, -57, -58, -58, -59,
    -60, -60, -61, -61, -62, -62, -63, -63, -63, -64, -64, -64, -64, -64, -64, -64,
    -64, -64, -64, -64, -64, -64, -64, -64, -63, -63, -63, -62, -62, -61, -61, -60,
    -60, -59, -58, -58, -57, -56, -55, -55, -54, -53, -52, -51, -50, -49, -48, -47,
    -46, -45, -43, -42, -41, -40, -39, -37, -36, -35, -33, -32, -31, -29, -28, -26,
    -25, -24, -22, -21, -19, -18, -16, -15, -13, -11, -10, -8, -7, -5, -4, -2,
    0, 1, 3, 4, 6, 7, 9, 10, 12, 14, 15, 17, 18, 20, 21, 23,
    24, 25, 27, 28, 30, 31, 32, 34, 35, 36, 38, 39, 40, 41, 42, 44,
    45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 54, 55, 56, 57, 57, 58,
    59, 59, 60, 60, 61, 61, 62, 62, 62, 63, 63, 63, 63, 63, 63, 63,
]
# [0x79CE] the 9 signed per-frame Yvel impulses of the jump arc (then gravity)
JUMP_IMPULSE = [-65, -51, -35, -20, -10, -5, -2, -1, 0]
# [0xA343] collectible score values, indexed by (collectible_id - 0x4A)
SCORE_VALUES = [120, 100, -90, 110, -120, 40, 80, 60, 10, 20, 30, 50, 60, 70, 75, 80, 100]
# [0x7190] 32 (x_half, y_half) sprite-hitbox extents, indexed by (sprite_id >> 8) & 0x1F
SPRITE_HALF_EXTENTS = [(40, 36), (32, 35), (24, 36), (32, 34), (32, 37), (32, 37), (32, 36), (32, 35), (32, 35), (32, 35), (24, 35), (32, 38), (40, 31), (48, 31), (32, 31), (32, 36), (24, 32), (40, 30), (40, 31), (40, 31), (32, 30), (24, 30), (32, 31), (24, 30), (24, 32), (40, 34), (32, 33), (32, 35), (32, 34), (40, 31), (40, 32), (40, 30)]
# [0x7B04] the 4 club-attack phases: (frame_table_ptr, sfx, v19, flag)
ATTACK_PHASES = [(31216, 2, 25, 0), (31282, 6, 30, 0), (31348, 6, 20, 1), (31420, 12, 30, 3)]
# [0x2301] keyboard make-code -> character ('-' = no char)
SCANCODE_CHARS = '--1234567890----A-E-----------A-DF------------C-B-----------------------------------'
# [CS 0x6AA9] object-AI dispatch table: handler CODE-entry addresses by object-type index
OBJECT_HANDLER_ADDRS = [31888, 31884, 31789, 31633, 31455, 31328, 30956, 30872, 30686, 30525, 30309, 30223, 30148, 32620, 32550, 32482, 32472, 32447, 32437]
# [0x000C] the boot resource/message records: (dgroup_offset, text) -- asset filenames + inline DOS strings
RESOURCE_RECORDS = [
    (0x000C, 'KEYB.SQZ'),
    (0x0015, 'ALLFONTS.SQZ'),
    (0x0022, 'FRONT.SQZ'),
    (0x002C, 'CASTLE.SQZ'),
    (0x003D, '+%'),
    (0x0840, 'FATAL: Not enough memory$SAMPLE.SQZ'),
    (0x0973, 'SORRY: Your Sound Blaster does not work correctly.$SORRY: There is not enough memory for SoundTracker Music.$PRES.TRK'),
    (0x09E9, 'CODE.TRK'),
    (0x09F2, 'CARTE.TRK'),
    (0x09FC, 'PRESENTA.TRK'),
    (0x0A09, 'GLACE.TRK'),
    (0x0A13, 'MAP.SQZ'),
    (0x0A1B, 'MOTIF.SQZ'),
    (0x0A25, 'UNION.SQZ'),
    (0x0A2F, 'MENU.SQZ'),
    (0x0A38, 'MINES.TRK'),
    (0x0A42, 'MYSTERY.TRK'),
    (0x0A4E, 'GAMEOVER.SQZ'),
    (0x0A5B, 'MENU2.SQZ'),
    (0x0A65, 'MONSTER.TRK'),
    (0x0A71, 'FINAL.TRK'),
    (0x0A7B, 'BRAVO.TRK'),
    (0x0A85, 'KOOL.TRK'),
    (0x0A8E, 'BOULA.TRK'),
]

# [0x752A] 32 sprite-hitbox X half-widths, indexed by (sprite_id >> 8) & 0x1F
HITBOX_HALF_WIDTHS = [20, 36, 16, 35, 12, 36, 16, 36, 16, 37, 16, 37, 15, 36, 16, 35,
                      15, 35, 15, 35, 12, 35, 16, 38, 20, 31, 24, 31, 16, 31, 16, 36]
# [0x7B7F] 3x8 table of anim-state ids selected by the input bitmask
ANIM_STATE_IDS = [0, 3, 5, 7, 2, 6, 0, 0, 1, 3, 4, 7, 2, 6, 1, 0, 1, 3, 4, 7, 2, 6, 0, 0]
# [0x7CDF] 9 word pointers into the 0x7Bxx anim-sequence data (DOS offsets; the bridge keeps them as words)
ANIM_SEQ_PTRS = [0x7B9F, 0x7BA7, 0x7BDB, 0x7BE7, 0x7BF7, 0x7C29, 0x7C3F, 0x7C53, 0x7C67]
