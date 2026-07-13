"""Symbolic DGROUP offset names — so shipped native code reads the real thing, not ``0x2DBC``.

Every constant here is a DS-relative offset into the game's global-data segment (DGROUP). They are the
offsets that survive as *raw accessor arguments* (e.g. a ``state`` word-read of ``SCROLL_SCRIPT_PTR``) because
they address arenas, pointer/segment cells, lookup tables, RNG state, or animation scratch that has no scalar
"field" home — everything else is a named view (``pre2/views/dgroup_view.py``). Each name carries its
``[asm ....]`` evidence anchor; where an offset is *also* exposed as a view field, the comment says so.

This is the readable spec for those offsets: the numbers live here, once, named — the call sites speak names.
Grouped by subsystem.
"""
from __future__ import annotations

# ---- the RNG (the 39DF LCG a/b/c/d state) --------------------------------------------------------------
RNG_STATE           = 0x2CEC   # the 4-byte LCG state: a [+0], b [+1], c [+2], d (word) [+3]=0x2CEF..0x2CF0
DECOR_RNG_TABLE     = 0x6CA9   # the 0x100-word decor table native_5237 fills from the carried RNG [5274..5284]
WALL_MARKER_TABLE   = 0x6EA9   # [0x6ea9..] = 0x55aa x 0x50 (the scenery wall markers) [asm 5287]

# ---- camera / scroll / the render-mirror scratch -------------------------------------------------------
CAM_COL             = 0x2DE4   # camera tile column, word (= PlayerGlobals.cam_col_word) [5ADF]
CAM_ROW             = 0x2DE6   # camera tile row, word (= PlayerGlobals.cam_row_word) [5ACD]
COL_RING            = 0x2DE8   # background column-ring index = camera_x % 0x14 (= col_ring) [frame]
ROW_RING            = 0x2DEA   # row-ring index = camera_y % 0x0C (= unk_2DEA)
PREV_CAM_CELL_X     = 0x2DE0   # render-mirror previous-camera cell X (also the 0x55aa grid-dirty token) [3b65]
PREV_CAM_CELL_Y     = 0x2DE2   # render-mirror previous-camera cell Y
SCROLL_COPY_SRC     = 0x2DBA   # the scroll-copy ring source build_background_ring lays tiles at (calc_scroll_source)
SCROLL_SCRIPT_PTR   = 0x2DBC   # the active per-level scroll-script pointer [asm 52b3]
SCROLL_SCRIPT_PTR2  = 0x2DBE   # its companion (reset to 0 alongside) [asm 52c2]
SCROLL_SCRIPT_TABLE = 0x2D40   # per-level scroll-script source table: ptr = [0x2D40 + level*2] [asm 52b3]
SCROLL_ANIM_CTR     = 0x2DF5   # a per-frame scroll/animation counter (+1 each 33C4) [asm 33C4]
CAM_SCROLL_IDLE     = 0x6BEE   # the scroll-converged flag pair (hi byte of the 0x6BED word) [3b38]

# ---- the level-end IRIS-close geometry (native_iris_close) ---------------------------------------------
IRIS_STEP           = 0x2DC0   # radius shrink step per frame (grows every 0x14 frames)
IRIS_ACCEL_CTR      = 0x2DC2   # the accel counter (bumps IRIS_STEP when it reaches 0x14)
IRIS_CLAMP          = 0x2DC4   # the max(2*Y, 0xF0) radius clamp
IRIS_CENTER_Y       = 0x2DC6   # iris centre Y (player screen Y)
IRIS_CENTER_X       = 0x2DC8   # iris centre X (player screen X)
IRIS_RADIUS         = 0x2DD0   # the current radius; != 0 -> native_render composes SceneKind.IRIS

# ---- per-level metadata + asset-addressing tables ------------------------------------------------------
WARP_TABLE          = 0x2CF6   # per-main-level -> bonus-level warp table (0x2CF6 + level) [4c8f]
PALETTE_PTR_TABLE   = 0x2D00   # per-level 16-colour palette pointer table: ptr = [0x2D00 + level*2]
SONG_INDEX_TABLE    = 0x2D20   # per-level song index: idx = [0x2D20 + level] [asm 01ab]
LEVEL_HEADER_TABLE  = 0x2D30   # per-level header size in paragraphs: [0x2D30 + level] [asm 4316]
FILENAME_DIGIT      = 0x2D90   # the LEVEL<n>.SQZ filename digit char [asm 3f26]
GFX_GROUP_TABLE     = 0x2D96   # per-level graphics-group id source: [0x2D96 + level]
GFX_GROUP           = 0x2DAA   # the committed graphics-group id (BACK<group>.SQZ) [asm 3f26]
LEVEL_DATA_SEG      = 0x2DDA   # the loaded level-data base segment (es for the tile grid) [asm 3f2b]
UNION_BANK_SEG      = 0x2DDC   # the shared/UNION sprite-bank base segment [asm 4389]
LOAD_TOP            = 0x2875   # the bump-allocator load top (the SQZ loads land here)

# ---- the collectible / score / item state -------------------------------------------------------------
COLLECT_TOTAL_MAIN  = 0x2A74   # the level's collectible total the tally reads (secret tiles += here) [3ed0]
COLLECT_TOTAL_DECOR = 0x2A78   # the decor-derived collectible count [asm 4073..40bb]
SCORE_MID_WORD      = 0x6C0C   # the score word zeroed alongside score_lo/hi on game-over [asm 5083-508f]
ITEM_QUEUE          = 0x6C12   # the collected-item queue / per-type count table (0x71 bytes) [asm 51e2]
ITEM_TOTAL          = 0x6C9E   # the collected-item total word [asm 51e9]

# ---- the animated-tile remap cycle (render_sync) ------------------------------------------------------
ANIM_REMAP_PTR      = 0x6BC2   # the animated-tile remap frame pointer (367D steps it per redraw)
ANIM_REMAP_THRESH   = 0x6BD4   # its threshold/counter byte

# ---- boss / level-init misc ---------------------------------------------------------------------------
BOSS_STATE          = 0xA517   # the boss state word, reset to 0xFFFF at level re-init [asm 52ad]
POPUP_RING_HEAD     = 0x6BBE   # the popup/message-ring head pointer, seeded to 0x4F76 [asm 5265]
LEVEL_INIT_FLAG_6BCC = 0x6BCC  # cleared at level-init [asm 01f5]; role not yet evidenced

# ---- the sound driver (native_play_sfx / native_load_sfx_bank) ----------------------------------------
SFX_DESC_SRC        = 0x1004   # the active SFX descriptor: PCM source offset [asm 02b7]
SFX_DESC_LEN        = 0x1006   # ... PCM length [asm 02be]
SFX_TABLE           = 0x1009   # the per-effect {src,len} table; entry = [0x1009 + dl*4] [asm 02a9]
SFX_PCSPK_NOTE      = 0x1035   # the PC-speaker active-note pointer [asm 0292]
SFX_SAMPLE_SEG      = 0x0B59   # the SFX PCM sample-bank segment [asm 07C9]
SONG_ORDER_LEN      = 0xDC2    # the loaded song's order-list length (NativeAudio fingerprint)
SONG_ORDER_TABLE    = 0xDC7    # the loaded song's order list

# ---- the light-fade state (the light pickups; render half in native_apply_palette_fade) ---------------
LIGHT_FADE_TO_DARK  = 0x6C01   # fade toward the dark "lights off" palette [876C]
LIGHT_FADE_TO_LEVEL = 0x6C02   # fade back toward the level palette [8790]
LIGHT_FADE_STEP     = 0x6C03   # the fade step counter

# ---- render page -------------------------------------------------------------------------------------
RENDER_PAGE         = 0x2DD8   # the CRTC render/back page the present flips to (cam.dest_page)

# ---- keyboard / input latches (the front-end busy-waits) ----------------------------------------------
FIRE_PRIMARY        = 0x27E8   # the primary fire/action key (the [0x27e8] | [0x2832] confirm)
IDLE_CLOCK          = 0x27F0   # the PIT-fed idle counter, low word (= idle_clock) [5DC9]
IDLE_CLOCK_HI       = 0x27F2   # its high word (the 32-bit free-running idle counter)
KEY_TABLE           = 0x27F4   # the residual key/scan table (0x80 bytes) cleared on scene entry
KEY_1_LATCH         = 0x27F6   # the '1' make-code latch (level select 1) [asm 9A64]
KEY_2_LATCH         = 0x27F7   # the '2' make-code latch (level select 2)
FIRE_LATCH          = 0x282D   # the fire/confirm make-code latch (paired with FIRE_SPACE)
FIRE_SPACE          = 0x2810   # the space/enter fire latch [asm]
FIRE_ALT            = 0x2832   # the secondary fire key (the [0x27e8] | [0x2832] confirm)
INPUT_READY_A       = 0x2805   # the 247B input-idle gate flags: al = [0x2811] & [0x282C] & [0x2805]
INPUT_READY_B       = 0x2811   # ... (all three must be set for the idle path)
INPUT_READY_C       = 0x282C   # ...
PENDING_KEY         = 0x2874   # the DC1 pending make-code (also the demo any-key) [asm 8eb6]

# ---- the attract demo-playback cursor -----------------------------------------------------------------
DEMO_CURSOR         = 0x287A   # the demo cursor + current byte/count (0x287A..0x287D) [asm 8eb6]
DEMO_LEVEL          = 0x83E    # the attract-demo's level source [asm 8ebb]
DEMO_MODE           = 0x83D    # the attract-demo's mode source [asm 8ec4]

# ---- the password / level-warp seed (932F) ------------------------------------------------------------
BIOS_SEED           = 0xA333   # the machine-fingerprint seed word (0x20 on the zeroed-BIOS GOG build) [932F]
SEED_COMPUTED_FLAG  = 0xA335   # the lazy-init "seed already computed" flag [asm 933c]
DECOR_PTR_LIST      = 0x6A88   # the decor-assignment pointer list (40bd bubble-sorts it) [asm 40bd]

# ---- the CARTE (world-map) marker geometry ------------------------------------------------------------
CARTE_MARKER_TABLE  = 0xB148   # the per-level marker offset table: entry = [0xB148 + lv*4] [asm 9562]
CARTE_MARKER_DIMS   = 0x7522   # the marker size (bytes-wide / rows) word [asm 9562]
CARTE_MASK_SEG      = 0x667A   # the marker mask far-pointer segment [0x667a]:[0x62da]
CARTE_MASK_OFF      = 0x62DA   # ... offset

# ---- the font + property-header words -----------------------------------------------------------------
FONT_SEG            = 0x3D     # the font segment pointer [0x3d]
LEVEL_PROP_HEADER   = 0x815E   # the property block start = the level's camera bottom-limit word [5722]
PLAYER_START_X      = 0x8160   # the level start point X (property header) -> player X [asm 4056]
PLAYER_START_Y      = 0x8162   # the level start point Y -> player Y

# ---- per-level index / mode / loader flags (also exposed as named views) ------------------------------
LEVEL_INDEX         = 0x2D8A   # the current level index (= PlayerGlobals.level)
MODE_COPY           = 0xB198   # the committed BEGINNER/EXPERT mode copy (= PlayerGlobals.mode_copy)
ANY_ANIMATED_FLAG   = 0x6BBD   # the loader's "any animated tile" flag (= PlayerGlobals.page_dirty) [asm 4311]
LEVEL_BOTTOM_LIMIT  = 0x2CF5   # the level header size / bottom camera limit byte [asm 4316 / 33B6]
SPRITE_BANK_LO      = 0x8C89   # the entity sprite-ref bank base A (= sprite_bank_lo view) [4182]
SPRITE_BANK_HI      = 0x8C8B   # ... bank base B (= sprite_bank_hi view; reset to 0x35/0x138 after rebase)

# ---- the horizontal-scroll follower (camera_scroll) ---------------------------------------------------
SCROLL_DIR          = 0x6BED   # the horizontal scroll direction: 1 = right, 2 = left [asm 57F6]
SCROLL_GATE_6BD9    = 0x6BD9   # a scroll-enable gate flag [asm 564E]
SCROLL_TARGET_X     = 0x6BF1   # the scroll target X the camera chases [asm]
SCROLL_WINDOW_FLAG  = 0x6BFE   # the in-window scroll-suppress flag [asm 57B9]
SCROLL_ACCUM        = 0x8164   # the scroll accumulator, reset on a snap-to-destination [asm 5335]
UNK_78C4            = 0x78C4   # role not yet evidenced (a scroll-adjacent word)
COMBO_COMPLETE_6BE2 = 0x6BE2   # set to 0x294 when the [0x6CA8] utensil group completes

# ---- camera shake / follow gates + bonus arming (loop) ------------------------------------------------
SHAKE_MAGNITUDE     = 0x6BEA   # the camera screen-shake magnitude [asm 026D / 4C30]
CAM_H_FOLLOW_GATE   = 0x8166   # gates the horizontal camera-follow (57A8 runs unless set) [asm 5643]
PENDING_PICKUP_6BE1 = 0x6BE1   # a pending-pickup / trigger-active flag matched vs the player tile [asm 549A]
REWARD_ARM_LO       = 0x6BFF   # the bonus-reward arm flag pair [asm 8D1B]
REWARD_ARM_HI       = 0x6C00   # ...
BONUS_LETTERS_MASK  = 0x6CA7   # the BONUS-letters collected mask (= PlayerGlobals.bonus_letters)
UTENSILS_MASK       = 0x6CA8   # the utensils collected mask (= PlayerGlobals.utensils_mask)
TERRAIN_ENTITY_BASE = 0x5570   # the terrain-entity projection record base [asm 5570]

# ---- the reward-burst (player_interaction 67DE) -------------------------------------------------------
BURST_POS_X         = 0xA336   # the reward-burst spawn position X = player X [asm 67DE]
BURST_POS_Y         = 0xA338   # ... position Y = player Y - 0x70 [asm 67E4]
BURST_SPRITE        = 0xA33A   # ... the reward sprite id (0x6E) [asm 67ED]

# ---- render / RNG scratch read by the memory adapters (views/) ----------------------------------------
RNG_ROTATE          = 0x28C1   # the one-word rotate-generator state (= ror view) [asm 26CF]
TILE_FLAGS_ACC      = 0x2DF2   # the tile-flags accumulator (VAR_TILE_FLAGS)
GRID_DIRTY          = 0x2DF4   # the whole-grid redraw request (= grid_dirty view) [asm 5C82]
BG_RESTORE_PTR      = 0x2DF6   # the background-restore source pointer (VAR_BG_PTR)
FIREFLY_SCRATCH_A   = 0x6BC0   # firefly-sim scratch byte A (= firefly_scratch_a view)
FIREFLY_SCRATCH_B   = 0x6BC1   # firefly-sim scratch byte B (= firefly_scratch_b view)
ANIM_GATE           = 0x6BD0   # hold-current-anim / FSM-route gate (= anim_gate view)
QUAKE_DIST_LO       = 0xA30E   # the boss-quake player-distance^2 scratch, low word (= quake_dist_lo view)
QUAKE_DIST_HI       = 0xA310   # ... high word (= quake_dist_hi view)
ANIM_READY          = 0xA340   # object_update's per-step anim-ready scratch byte (= anim_ready view)

# ---- the free-running frame timer ---------------------------------------------------------------------
FRAME_TIMER         = 0x6BD5   # the free-running frame counter 26FA bumps (= frame_stamp / frame_blink view)
ROW_FACTOR          = 0x6BF8   # the row-stride factor the camera shake writes (= PlayerGlobals.row_factor)

# ---- bulk state blocks + lookup tables (referenced as slice bounds) -----------------------------------
TIMER_STATE_BLOCK   = 0x6BC4   # the timer/state block zeroed at re-init (starts at fine_scroll) [asm 5247]
FINE_SCROLL         = 0x6BC4   # scalar alias of the block start: the sub-tile fine-scroll accumulator byte
DBL_BUFFER          = 0x815E   # the working double-buffer block (the property header lives at +2/+4) [5251]
DBL_BUFFER_BACKUP   = 0x9203   # the pristine double-buffer backup 5237 restores from [asm 5251]
FILL_BLOCK_7DE6     = 0x7DE6   # a 0xFF-filled state block [asm 5295]
FILL_BLOCK_7DAF     = 0x7DAF   # a 0xFF-filled state block [asm 52a0]
TILE_TYPE_TABLE     = 0x4DF8   # per-cache-slot tile type table (0 opaque / 1 empty / ...) [asm 4232]
TILE_MASK_TABLE     = 0x2DF8   # the compacted partial-transparency masks the per-frame pass reads [asm 4232]
ROW_SINE_TABLE      = 0x6F90   # the row-bounce / bird-orbit signed sine table [asm 9B00]
GAMEOVER_PALETTE    = 0xAFE8   # the game-over diorama 16-colour palette (DAC load) [asm 9B7F]
MENU_MORPH_SRC      = 0xACE7   # the static menu palette the CODE-screen morph targets

# ---- arena bases walked by index (stride noted) -------------------------------------------------------
ENTITY_LIST_2NDPASS = 0x8489   # the second-pass entity list (variable stride = [si]) [asm 4182]
BONUS_CELL_LIST     = 0x8C8D   # the 0x50-entry bonus/secret-cell list (stride 5) [asm 3eb2]
ACTIVE_FLAG_SNAPSHOT = 0xA2A8  # the respawn active-flag snapshot table (0x50 bytes) [asm 4fa7]
BIRD_PAIR_SLOTS     = 0x4FD0   # the game-over bird paired render slots (stride 0x12) [9D0C]

# ---- render / effect slots (the stride-0x12 records; see dgroup_view PlayerView/RenderSlot) ------------
PLAYER_Y            = 0x4F1E   # the player record Y word (= PlayerView.y)
PLAYER_MOVE_FLAG    = 0x4F23   # the player facing/move flag byte; & 0x80 = moving left [asm 57FD]
RENDER_SLOTS_BASE   = 0x4F0A   # slot 0; the player is slot 1 (base + 0x12 = 0x4F1C) [= dgroup_view]
SLOT0_SPRITE        = 0x4F0E   # slot-0 sprite word: 0xFFFF suppresses the normal player draw [50DF]
PLAYER_SLOT         = 0x4F1C   # the player record (= dgroup_view PLAYER_BASE / PlayerView base)
PROJECTILE_SLOTS    = 0x4F2E   # the 4-slot projectile / attract-dino / tally-food list (stride 0x12) [627D]
POT_FLAME_SPRITE    = 0x4F56   # the tally pot-flame render-slot sprite id [asm 51F0]
EFFECT_SPRITE_SRC   = 0x8F1D   # the float-effect sprite-source list (0x46 x stride 7) [414d]
FALLING_FOOD_SLOTS  = 0x52E8   # the 20 falling-food effect slots (stride 0x12) the count-up scans [4E53]
CRY_CAVEMAN_SPRITE  = 0x5088   # the game-over crying-caveman sprite slot [9CD8]
