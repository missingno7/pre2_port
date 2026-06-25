"""FaithfulSession — the faithful video backend, extracted from ``scripts/play._run_view``.

This is a **pure behaviour-preserving move**: it owns the faithful renderer's capture state, the VM-event
capture hooks that feed it, the scene composition, and the gap diagnostics — everything that used to live as
closure cells + ``_faithful_planar`` inside ``_run_view``. ``play.py`` now only constructs it, installs its
hooks, and asks it for a frame (``frame(mem)``). No logic was redesigned and no faithful output changed.

It is the correctness baseline the future ``EnhancedRenderer`` will sit on top of: the enhanced renderer will
consume ``frame()`` / a grounded state snapshot and must NEVER touch the VM framebuffer. This session is the
ONLY thing that turns recovered leaves into the displayed faithful image; there is no VM-framebuffer fallback
anywhere on this path (unrecovered scenes fail loud with :class:`FaithfulVisualGap` context).

Imported lazily from ``_run_view`` (it pulls ``scripts/sdl_view``, kept out of module-load like play.py does).
"""
from __future__ import annotations

import os

import numpy as np

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from sdl_view import render_planar_rgb, render_planar_rgb_from_planes
from pre2.bridge.game_visual_state import capture_game_visual_state, render_game_visual_state
from pre2.bridge.live_render import compose_curtain_planes, compose_vfade_planes, render_visual_planes
from pre2.bridge.particles import read_particles
from pre2.bridge.foreground_tiles import read_foreground_state
from pre2.bridge.gameplay_effects import apply_gameplay_effects, capture_gameplay_effects
from pre2.bridge.gameover_scene import build_gameover_scene, load_gameover_asset, _object_overlay
from pre2.bridge.render_state import read_renderer_state, retarget_page
from pre2.bridge.tally_scene import build_tally_scene
from pre2.bridge.oldies_scene import build_oldies_scene
from pre2.bridge.tally_panel import read_tally_panel
from pre2.bridge.image_scene import identify_image, render_image_scene
from pre2.bridge.scene_state import derive_scene_kind
from pre2.bridge import present as _present_bridge
from pre2.bridge import text as _text_bridge
from pre2.recovered.gameover_background import render_gameover_background
from pre2.recovered.carte import build_carte_page
from pre2.recovered.menu_scene import MenuScenePage
from pre2.recovered.scene_compositor import RecoveredBackground
from pre2.recovered.faithful_visual import FaithfulVisualGap, SceneKind
from pre2.enhanced.extract import extract_enhanced_frame

_DSEG = 0x1A0F
_HUD_OFF = 176 * 0x28      # HUD strip start within a page (row 176)
_HUD_LEN = 24 * 0x28       # rows 176..199 (status bar + dynamic glyphs)

# frame() return sentinel: the display is BLANKED (palette load) -> the caller must NOT present (it keeps the
# previous frame on screen), exactly as the original render_current did (it returned early before presenting).
BLANK_NO_PRESENT = object()


class FaithfulSession:
    """Owns the live faithful renderer: capture hooks + scene composition + gap diagnostics."""

    def __init__(self, rt, args, *, verify=False):
        self.rt = rt
        self.args = args
        self.dos = rt.dos
        self.verify = verify
        # --- faithful capture/scene state (was closure cells in _run_view) ---
        self.faithful_info = ""        # title-bar note for the live faithful renderer
        self.gap_seen = None
        self.boundary_capture = None   # (rgb, page, scene_kind_name, verify_Δ|None) from the last 6772 commit
        self.curtain_cache = None      # new-room planes (at src page) rendered once per curtain reveal (3054)
        self.last_committed = None     # (planes, page) of the last 6772 frame — base for the vertical fade-out
        self.particle_frame = None     # ParticleFrame snapshotted at 4b8e entry (one-shot; gone by 6772)
        self.foreground_frame = None   # ForegroundState snapshotted at 3732 entry (cleared by 6772)
        self.gameover_pending = None   # (scroll, page) stashed at the 9C87 diorama present
        self.tally_pending = None      # TallyPanelInputs stashed at the 51A3 driver
        self.scene_capture = None      # (rgb, page, ic, label) of the last complete recovered SCENE frame
        self.oldies_capture = None     # (planes, page) of the OLDIES easter-egg scene (static -> planes)
        self.carte_capture = None      # (asset_bytes, scroll_x, ic) of the CARTE/map scroll-in
        self.scroll_shift_ic = -1 << 30   # ic when scroll_shift (9804) last fired (carte vs stateful menu)
        self.menu_page = None          # the recovered MenuScenePage (stateful)
        self.menu_active = False       # is the menu controller running (seed 9725 -> ret 9885)
        self.menu_active_ic = -1 << 30  # ic of the last menu event
        self.held_planes = None        # (planes, page, pel, active_w, wrap, tick) of the last pan-scene
        self.faithful_tick = 0         # increments per faithful planar render (wall-clock-independent grace)
        self.planar_image_capture = None  # 4 EGA planes of a 0Dh PLANAR image (attract title etc.)
        self.current_13h_image = None  # (asset name, has_logo) of the mode-13h image on screen
        self.last_capture_ic = 0       # ic at the last 6772 capture (staleness for the death spin)
        self.last_hud = None           # (4 HUD-strip plane slices) from the last 6772 commit
        self.last_gp_ic = 0            # ic when a GAMEPLAY/IRIS frame was last DISPLAYED
        # --- capture-hook originals (chained live replacements), filled by install_hooks ---
        self._orig = {}
        try:
            self._go_asset = load_gameover_asset(args.game_root)
        except Exception:
            self._go_asset = None
        # --- enhanced (modern RGB/RGBA) source-snapshot seam (--video enhanced only) ---
        # Captured ONLY at the gameplay source-frame commit (6772), kept as prev+cur for the enhanced
        # compositor to interpolate. enh_clock() timestamps each commit (wall clock in live --view); when
        # unset (deterministic/headless) enhanced capture is off.
        self.enhanced_capture = False
        self.enh_clock = None
        self.enh_prev = None
        self.enh_cur = None
        self.enh_prev_time = 0.0
        self.enh_cur_time = 0.0

    # -------------------------------------------------------------------- helpers
    def _rw(self, mem, off):
        b = ((_DSEG << 4) + off) & 0xFFFFF
        return mem.data[b] | (mem.data[b + 1] << 8)

    def _snapshot_hud(self, planes, page):
        o = (page + _HUD_OFF) & 0xFFFF
        return [bytes(planes[p][o:o + _HUD_LEN]) for p in range(4)]

    def _overlay_hud(self, planes, page, hud):
        o = (page + _HUD_OFF) & 0xFFFF
        for p in range(4):
            planes[p][o:o + _HUD_LEN] = hud[p]

    # -------------------------------------------------------------------- hook install
    def install_hooks(self):
        """Register all faithful capture hooks on the CPU (faithful mode only). Each captures its existing
        replacement (so live replacements still run) and chains to it; the unconditional ones interpret the
        ASM instruction when there is no prior replacement."""
        rt = self.rt
        rt.cpu.pre2_dos = rt.dos

        def reg(off, fn, name):
            bnd = (0x1030, off)
            self._orig[off] = rt.cpu.replacement_hooks.get(bnd)
            rt.cpu.replacement_hooks[bnd] = fn
            rt.cpu.hook_names[bnd] = name

        def reg_chain_required(off, fn, name):
            """Only install if a prior replacement exists (these CHAIN a required live replacement)."""
            bnd = (0x1030, off)
            orig = rt.cpu.replacement_hooks.get(bnd)
            self._orig[off] = orig
            if orig is not None:
                rt.cpu.replacement_hooks[bnd] = fn
                rt.cpu.hook_names[bnd] = name

        reg(0x6772, self._capture_at_boundary, "palette_fade+faithful_capture")
        reg(0x307D, self._capture_curtain_step, "curtain_step+faithful_capture")
        reg(0x3111, self._capture_vfade_step, "vfade_step+faithful_capture")
        reg(0x4B8E, self._capture_particles, "particles_capture")
        reg(0x3732, self._capture_foreground, "foreground_capture")
        reg(0x9C87, self._capture_gameover_present, "gameover_present_capture")
        reg(0x51A3, self._mark_tally, "tally_driver_mark")
        reg(0x2417, self._mark_oldies, "oldies_mark")
        reg(0x44FB, self._capture_scene_flip, "scene_flip_capture")
        reg(0x91C0, self._identify_13h, "image13h_identify")
        reg(0x90C0, self._mark_13h_logo, "image13h_logo")
        reg_chain_required(0x965A, self._mark_carte, "carte_blit_mark")
        reg(0x9725, self._menu_seed, "menu_seed")
        reg(0x9885, self._menu_exit, "menu_exit")
        reg_chain_required(0x9804, self._mark_scroll_shift, "carte_shift_mark")
        reg_chain_required(0x9886, self._menu_text, "menu_text_mark")
        reg(0x9169, self._capture_planar_image, "planar_image_capture")

    # -------------------------------------------------------------------- hooks
    def _capture_at_boundary(self, c):
        rt = self.rt
        try:
            disp = rt.program.memory.ega_display_start
            fx = capture_gameplay_effects(c.mem, particle_frame=self.particle_frame,
                                          foreground_frame=self.foreground_frame)
            gvs = capture_game_visual_state(c.mem, c.pre2_dos, disp,
                                            game_root=self.args.game_root, effects=fx)
            planes, page = render_game_visual_state(gvs)       # raises FaithfulVisualGap for scenes
            d = None
            if self.verify:
                data = rt.program.memory.data; d = 0
                for p in range(4):
                    apb = EGA_APERTURE + p * EGA_PLANE_STRIDE
                    for o in range(176 * 0x28):                # gameplay viewport (HUD verified separately)
                        a = (page + o) & 0xFFFF
                        if planes[p][a] != data[apb + a]:
                            d += 1
            self.boundary_capture = (render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette),
                                     page, gvs.scene_kind.name, d)
            self.last_committed = (planes, page)  # base for the vertical fade-out (the frame it clears)
            self.last_capture_ic = rt.cpu.instruction_count
            self.planar_image_capture = None      # a committed gameplay frame -> the title image is gone
            self.last_hud = self._snapshot_hud(planes, page)  # the DISPLAYED HUD (frozen between commits)
            if self.enhanced_capture and self.enh_clock is not None:
                # Source-snapshot seam: extract the modern RGB/RGBA frame at THIS gameplay commit (the only
                # place a new source frame is produced ~25 fps) and keep prev+cur for the enhanced compositor.
                try:
                    efs = extract_enhanced_frame(c.mem, self.dos, game_root=self.args.game_root,
                                                 with_faithful=False, effects=fx)
                    if efs is not None:
                        self.enh_prev, self.enh_cur = self.enh_cur, efs
                        self.enh_prev_time, self.enh_cur_time = self.enh_cur_time, self.enh_clock()
                except Exception:
                    pass
        except FaithfulVisualGap:
            self.boundary_capture = None           # a SCENE/IMAGE frame at 6772 -> handled at present time
        except Exception:
            self.boundary_capture = None
        self.curtain_cache = None                  # the per-frame boundary ends any curtain in progress
        self.particle_frame = None                 # consumed for this frame; 4b8e re-stashes next frame
        self.foreground_frame = None               # consumed; 3732 re-stashes next frame it runs
        orig = self._orig[0x6772]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)          # no palette hook -> run the ASM instr

    def _capture_curtain_step(self, c):
        try:
            src = self._rw(c.mem, 0x2DD8)
            dst = self._rw(c.mem, 0x2DD6)
            step = c.mem.data[(0x1030 << 4) + 0x3050] | (c.mem.data[(0x1030 << 4) + 0x3051] << 8)
            completed = step // 4 + 1                       # strip-pairs done by this 307D
            if self.curtain_cache is None:
                nr, _, kind = render_visual_planes(c.mem, c.pre2_dos, game_root=self.args.game_root,
                                                   display_page=src)
                self.curtain_cache = (nr, src, kind.name)
            nr, csrc, kindname = self.curtain_cache
            planes, page = compose_curtain_planes(nr, csrc, dst, completed)
            if self.last_hud is not None:
                self._overlay_hud(planes, page, self.last_hud)
            self.boundary_capture = (
                render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette),
                page, kindname, None)
        except Exception:
            pass
        orig = self._orig[0x307D]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _capture_vfade_step(self, c):
        try:
            if self.last_committed is not None:
                bplanes, bpage = self.last_committed
                page = self._rw(c.mem, 0x2DD6)
                s52 = c.mem.data[(0x1030 << 4) + 0x3052] | (c.mem.data[(0x1030 << 4) + 0x3053] << 8)
                s50 = c.mem.data[(0x1030 << 4) + 0x3050] | (c.mem.data[(0x1030 << 4) + 0x3051] << 8)
                top = (s52 - page) // 0x28 + 10            # top band accumulated extent
                bot = (s52 + s50 - page) // 0x28           # bottom band start
                planes, pg = compose_vfade_planes(bplanes, bpage, top, bot)
                self.boundary_capture = (
                    render_planar_rgb_from_planes(planes, pg, c.pre2_dos.vga_palette),
                    pg, "GAMEPLAY", None)
                if top >= bot:
                    self.last_committed = (planes, pg)
        except Exception:
            pass
        orig = self._orig[0x3111]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _capture_particles(self, c):
        try:
            pf = read_particles(c.mem)
            self.particle_frame = pf if pf.particles else None
        except Exception:
            self.particle_frame = None
        orig = self._orig[0x4B8E]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _capture_foreground(self, c):
        try:
            self.foreground_frame = read_foreground_state(c.mem)
        except Exception:
            self.foreground_frame = None
        orig = self._orig[0x3732]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _capture_gameover_present(self, c):
        try:
            scroll = c.mem.data[(0x1A0F << 4) + 0x6BC4]
            page = c.mem.data[(0x1A0F << 4) + 0x2DD8] | (c.mem.data[(0x1A0F << 4) + 0x2DD9] << 8)
            self.gameover_pending = (scroll, page)
        except Exception:
            self.gameover_pending = None
        orig = self._orig[0x9C87]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _mark_tally(self, c):
        try:                                   # stash the panel state AS DRAWN (the % counts up after)
            self.tally_pending = read_tally_panel(c.mem)
        except Exception:
            self.tally_pending = None
        orig = self._orig[0x51A3]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _mark_oldies(self, c):
        try:
            page = c.mem.data[(0x1A0F << 4) + 0x2DD6] | (c.mem.data[(0x1A0F << 4) + 0x2DD7] << 8)
            planes, _st = build_oldies_scene(c.mem, page=page)
            self.oldies_capture = (tuple(bytes(pl) for pl in planes), page)
        except Exception:
            pass
        orig = self._orig[0x2417]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _capture_scene_flip(self, c):
        # At the page flip the back page holds a complete frame. Render the recovered SCENE for the screen
        # that drew it: game-over (9C87 diorama present ran) or tally (51A3 panel driver ran).
        rt = self.rt
        try:
            if self.gameover_pending is not None and self._go_asset is not None:
                scroll, page = self.gameover_pending
                bg = RecoveredBackground(tuple(bytes(pl) for pl in
                                                render_gameover_background(self._go_asset, scroll, page)))
                planes, _st = build_gameover_scene(c.mem, rt.dos, game_root=self.args.game_root,
                                                   page=page, background=bg)
                if self.last_hud is not None:        # the displayed game-over HUD is FROZEN at death
                    self._overlay_hud(planes, page, self.last_hud)
                rgb = render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette)
                self.scene_capture = (rgb, page, rt.cpu.instruction_count, "GAMEOVER")
            elif self.tally_pending is not None:
                page = c.mem.data[(0x1A0F << 4) + 0x2DD8] | (c.mem.data[(0x1A0F << 4) + 0x2DD9] << 8)
                planes, _st = build_tally_scene(c.mem, rt.dos, game_root=self.args.game_root, page=page,
                                                panel_inputs=self.tally_pending)
                rgb = render_planar_rgb_from_planes(planes, page, c.pre2_dos.vga_palette)
                self.scene_capture = (rgb, page, rt.cpu.instruction_count, "TALLY")
        except Exception:
            pass
        self.gameover_pending = None
        self.tally_pending = None
        orig = self._orig[0x44FB]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _identify_13h(self, c):
        try:
            src = ((c.s.ds << 4) + c.s.si) & 0xFFFFF
            head = bytes(c.mem.data[src:src + 256])
            name = identify_image(head, self.args.game_root)
            if name is None and os.environ.get("PRE2_GAP_DUMP"):
                import hashlib as _h
                fn = f"artifacts/img13h_unid_{_h.sha256(head).hexdigest()[:8]}.bin"
                if not os.path.exists(fn):
                    open(fn, "wb").write(bytes(c.mem.data[src:src + 0x10000]))
                    print(f"[img13h] unidentified 13h source -> {fn}", flush=True)
            if name is not None:
                prev = self.current_13h_image
                if not isinstance(prev, tuple) or prev[0] != name:
                    self.current_13h_image = (name, False)
            else:
                self.current_13h_image = False
        except Exception:
            pass
        orig = self._orig[0x91C0]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _mark_13h_logo(self, c):
        if self.current_13h_image is not None:
            self.current_13h_image = (self.current_13h_image[0], True)
        orig = self._orig[0x90C0]
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)

    def _mark_carte(self, c):
        try:
            sx, source = _present_bridge.read_scroll_inputs(c.mem)
            self.carte_capture = (source, sx, self.rt.cpu.instruction_count)
        except Exception:
            pass
        return self._orig[0x965A](c)

    def _menu_seed(self, c):
        try:
            seg = c.mem.data[(0x1A0F << 4) + 0x2875] | (c.mem.data[(0x1A0F << 4) + 0x2876] << 8)
            asset = bytes(c.mem.data[seg << 4:(seg << 4) + 0x4000])
            mp = MenuScenePage()
            mp.seed(asset)
            self.menu_page = mp
            self.menu_active = True
            self.menu_active_ic = self.rt.cpu.instruction_count
            self.planar_image_capture = None  # the attract title image is gone once a menu seeds
            self.oldies_capture = None         # the cold-boot attract OLDIES is over once a menu seeds
        except Exception:
            self.menu_page = None
        interpret_current_instruction_without_hook(c)

    def _menu_exit(self, c):
        self.menu_active = False
        interpret_current_instruction_without_hook(c)

    def _mark_scroll_shift(self, c):
        self.scroll_shift_ic = self.rt.cpu.instruction_count
        if self.menu_active and self.menu_page is not None:
            try:
                b199, sx, sy, psy, pd = _present_bridge.read_scroll_shift_inputs(c.mem)
                self.menu_page.scroll_shift(b199, sx, sy, psy, pd, wrap=c.s.bp)
                self.menu_active_ic = self.rt.cpu.instruction_count
            except Exception:
                pass
        return self._orig[0x9804](c)

    def _menu_text(self, c):
        if self.menu_active and self.menu_page is not None:
            try:
                ti = _text_bridge.read_text_inputs(c.mem, c.s.ds, c.s.bx)
                self.menu_page.stamp_text(ti.text, ti.font, ti.font_base, ti.pen, ti.advance,
                                          ti.page_draw, ti.page_clear)
                self.menu_active_ic = self.rt.cpu.instruction_count
            except Exception:
                pass
        return self._orig[0x9886](c)

    def _capture_planar_image(self, c):
        try:
            src = ((c.s.ds << 4) + c.s.si) & 0xFFFFF
            raw = bytes(c.mem.data[src:src + 4 * 0x1F40])
            self.planar_image_capture = tuple(
                bytes(raw[p * 0x1F40:(p + 1) * 0x1F40]) for p in range(4))
        except Exception:
            self.planar_image_capture = None
        interpret_current_instruction_without_hook(c)

    # -------------------------------------------------------------------- composition
    def frame(self, mem_bytes):
        """Compose the faithful RGB frame for the CURRENT VM video state (text marker / 13h image / planar
        scene), or ``None`` for an unknown mode (caller blanks the screen). Sets :attr:`faithful_info`. Never
        reads the VM framebuffer — every pixel comes from a recovered leaf."""
        rt = self.rt
        mode = self.dos.video_mode & 0x7F
        self.faithful_info = ""
        if mode not in (0x13, 0x19):
            # Outside 13h, forget the on-screen 13h image so the NEXT 13h scene starts from "nothing loaded
            # yet" (-> black while it loads) and re-identifies on its own 91C0 copy.
            self.current_13h_image = None
        else:
            self.planar_image_capture = None   # left 0Dh -> the 0Dh planar title image is no longer on screen
        if not rt.program.memory.ega_display_enabled:
            # Attribute controller has the display blanked (PAS=0) during a palette load. The original
            # render_current returned early here WITHOUT presenting (the screen keeps the previous frame
            # rather than flashing the incoming screen with the old/partial palette) -> signal that.
            self.faithful_info = "display blanked (palette load)"
            return BLANK_NO_PRESENT
        if mode in (0, 1, 2, 3, 7):
            # A DOS text mode is not game content -> explicit marker (never the ASM text VRAM).
            self.faithful_info = "faithful: DOS text mode (not game content)"
            return np.full((200, 320, 3), (16, 16, 24), dtype=np.uint8)
        if mode in (0x13, 0x19):
            return self._frame_13h()
        if rt.program.memory.ega_planar:
            mem_o = rt.program.memory
            ds = mem_o.ega_pan_display_start if mem_o.ega_pan_active else mem_o.ega_display_start
            return self._faithful_planar(mem_bytes, ds)
        return None    # unknown mode -> caller blanks the screen

    def _frame_13h(self):
        # FAITHFUL 13h: re-render the recovered image (identified at the 91C0 copy) from the decoded asset +
        # the live DAC palette -- NEVER read the A000 framebuffer. An unidentified image fails LOUD (gap).
        cur = self.current_13h_image
        rgb = None
        if isinstance(cur, tuple):
            name, has_logo = cur
            try:
                img = render_image_scene(name, self.args.game_root, with_logo=has_logo)
                pal = np.array(self.dos.vga_palette or [(0, 0, 0)] * 256, dtype=np.uint8)
                rgb = pal[np.frombuffer(img, dtype=np.uint8).reshape(200, 320)]
                self.faithful_info = f"faithful[IMAGE:{name}]"
            except Exception:
                rgb = None
        if rgb is None:
            if cur is None:
                # 13h mode but NO image copied yet (mode just switched / loading) -> black is correct here.
                self.faithful_info = "faithful: 13h loading"
                rgb = np.zeros((200, 320, 3), dtype=np.uint8)
            else:
                # cur is False = a 13h image WAS copied but unrecognised (genuinely unrecovered) -> fail LOUD.
                if self.gap_seen != "13h":
                    self.gap_seen = "13h"
                    print("[faithful] mode-13h image not identified (no recovered leaf yet)", flush=True)
                self.faithful_info = "FAITHFUL GAP: 13h image (see console)"
                rgb = np.full((200, 320, 3), (48, 0, 32), dtype=np.uint8)
        return rgb

    def _faithful_planar(self, mem_bytes, ds):
        """Mirror the committed frame from the 1030:6772 frame-boundary GameVisualState capture (NOT an
        ad-hoc live read). Gameplay/iris frames come from the latest boundary capture; scenes whose leaf is
        not recovered yet fail LOUD (diagnostic frame + console hint), never ASM VRAM."""
        rt = self.rt
        self.faithful_tick += 1
        cur_kind = derive_scene_kind(rt.cpu.mem, rt.dos)
        # Gameplay AT THE CAMERA ORIGIN reads as SCENE (is_gameplay_frame keys on a non-zero camera). The
        # authoritative "we are in gameplay" signal is the 1030:6772 main-loop boundary having fired recently
        # (scenes run their own loops, never 6772); reclassify so it routes to the gameplay branch.
        if cur_kind == SceneKind.SCENE and rt.cpu.instruction_count - self.last_capture_ic < 200000:
            cur_kind = SceneKind.GAMEPLAY
        if cur_kind in (SceneKind.GAMEPLAY, SceneKind.IRIS):
            # Long gap with no 6772 commit (e.g. the player-death fall sub-loop) would FREEZE the viewer on
            # the last capture. When the VM idles in the per-frame governor spin (1C6F-1C7E) the displayed
            # frame IS committed + render-consistent (render_frame Δ=0 there), so render LIVE instead of
            # freezing. Gated on staleness so normal gameplay always uses the clean 6772 capture.
            ip = rt.cpu.s.ip
            if (rt.cpu.instruction_count - self.last_capture_ic > 30000
                    and (rt.cpu.s.cs & 0xFFFF) == 0x1030 and 0x1C6F <= ip <= 0x1C7E):
                try:
                    disp = rt.program.memory.ega_display_start
                    planes, page, k = render_visual_planes(rt.cpu.mem, rt.dos,
                                                           game_root=self.args.game_root, display_page=disp)
                    apply_gameplay_effects(planes, page, capture_gameplay_effects(
                        rt.cpu.mem, particle_frame=self.particle_frame,
                        foreground_frame=self.foreground_frame))
                    if self.last_hud is not None:
                        self._overlay_hud(planes, page, self.last_hud)
                    rgb = render_planar_rgb_from_planes(planes, page, rt.dos.vga_palette)
                    self.boundary_capture = (rgb, page, k.name, None)
                    self.last_committed = (planes, page)
                    self.last_gp_ic = rt.cpu.instruction_count
                    self.faithful_info = f"faithful[{k.name}]@spin(live)"
                    return rgb
                except Exception:
                    pass
            cap = self.boundary_capture
            if cap is not None and cap[2] in ("GAMEPLAY", "IRIS"):
                rgb, page, kindname, d = cap
                if self.verify and d is not None:
                    self.faithful_info = f"faithful[{kindname}]@6772 Δ={d}" + ("" if d <= 96 else " !!")
                else:
                    self.faithful_info = f"faithful[{kindname}]@6772"
                self.last_gp_ic = rt.cpu.instruction_count
                return rgb
            self.faithful_info = "faithful: awaiting 6772 boundary capture"
            return np.full((200, 320, 3), (48, 0, 32), dtype=np.uint8)
        # Recovered SCENE (game-over diorama / tally panel), captured at the page flip.
        if self.scene_capture is not None and rt.cpu.instruction_count - self.scene_capture[2] < 200000:
            self.faithful_info = f"faithful[{self.scene_capture[3]}]@flip"
            return self.scene_capture[0]
        # MODE-SELECT MENU (0Dh, panning). The recovered MenuScenePage owns the stateful page; deplanarize it
        # with the live CRTC pan. Gate on the controller-active flag (NOT ic-freshness): the menu's own fade
        # runs while menu_active is True with the producers paused, so an ic gate would wrongly drop to a gap.
        if self.menu_active and self.menu_page is not None and rt.program.memory.ega_pan_active:
            pel = rt.program.memory.ega_pan_pel
            active_w = (rt.program.memory.ega_h_display_end + 1) * 8
            self.faithful_info = "faithful[MENU]"
            self.last_gp_ic = rt.cpu.instruction_count
            self.held_planes = (self.menu_page.planes, ds, pel, active_w, 0x1FFF, self.faithful_tick)
            return render_planar_rgb_from_planes(self.menu_page.planes, ds, rt.dos.vga_palette,
                                                 pel, active_w, wrap=0x1FFF)
        # CARTE / map scroll-in (0Dh, panning). Rebuilt from the captured asset + scroll_x (build_carte_page).
        # Gated on a FRESH scroll-blit capture AND no recent scroll_shift (that 9804 self-copy = the stateful
        # menu, which build_carte_page does not model). 200k ic window (one LIVE frame can be ~100k+ ic).
        if (self.carte_capture is not None and rt.program.memory.ega_pan_active
                and rt.cpu.instruction_count - self.carte_capture[2] < 200000
                and rt.cpu.instruction_count - self.scroll_shift_ic > 200000):
            asset, sx, _ic = self.carte_capture
            planes = build_carte_page(asset, sx)
            pel = rt.program.memory.ega_pan_pel
            active_w = (rt.program.memory.ega_h_display_end + 1) * 8
            self.faithful_info = f"faithful[CARTE]@{sx}"
            self.last_gp_ic = rt.cpu.instruction_count
            self.held_planes = (planes, ds, pel, active_w, 0x1FFF, self.faithful_tick)
            return render_planar_rgb_from_planes(planes, ds, rt.dos.vga_palette, pel, active_w, wrap=0x1FFF)
        # 0Dh PLANAR IMAGE (the attract title). Captured at 9153/9169 from the decoded asset; deplanarize with
        # the LIVE palette so the fade-in renders. Background at the displayed page + the live object overlay.
        if self.planar_image_capture is not None and rt.program.memory.ega_planar:
            page = ds & 0xFFFF
            planes = [bytearray(0x10000) for _ in range(4)]
            for p in range(4):
                planes[p][page:page + 0x1F40] = self.planar_image_capture[p]
            try:
                rs = retarget_page(read_renderer_state(rt.cpu.mem, rt.dos, game_root=self.args.game_root), page)
                _object_overlay(rs)(planes, page)
            except Exception:
                pass            # no/!ready object state -> just the background (e.g. the title before chars)
            active_w = (rt.program.memory.ega_h_display_end + 1) * 8
            self.faithful_info = "faithful[TITLE]"
            self.held_planes = (planes, page, 0, active_w, 0xFFFF, self.faithful_tick)
            return render_planar_rgb_from_planes(planes, page, rt.dos.vga_palette, 0, active_w)
        # Held pan-scene grace: between scenes the engine DAC-fades; the producer pauses + the gates drop. The
        # planes are unchanged through a DAC fade, so re-deplanarize them with the LIVE (fading) palette. The
        # frame-tick bound (wall-clock independent) still fails loud on a genuinely persistent unrecovered scene.
        if self.held_planes is not None and self.faithful_tick - self.held_planes[5] < 150:
            hp, hpg, hppel, hpaw, hpwrap, _t = self.held_planes
            self.faithful_info = "faithful: holding (scene fade)"
            return render_planar_rgb_from_planes(hp, hpg, rt.dos.vga_palette, hppel, hpaw, wrap=hpwrap)
        # SCENE / IMAGE. A brief blip to SCENE/IMAGE during a gameplay transition must NOT flash the
        # placeholder. Hold the last gameplay frame for a short grace; only fail loud if the scene PERSISTS.
        if (rt.cpu.instruction_count - self.last_gp_ic < 90000 and self.boundary_capture is not None):
            self.faithful_info = "faithful: holding (transition)"
            return self.boundary_capture[0]
        # OLDIES easter egg (static glyph-text, 0Dh, no scroll). ega_pan_active distinguishes it from the
        # scrolling menu/map. Render the captured planes with the LIVE palette so the fade-in is reproduced.
        if self.oldies_capture is not None and not rt.program.memory.ega_pan_active:
            planes, page = self.oldies_capture
            self.faithful_info = "faithful[OLDIES]"
            return render_planar_rgb_from_planes(planes, page, rt.dos.vga_palette)
        # persistent unrecovered scene -> fail loud (no ASM VRAM fallback). Dedup on a context signature.
        m = rt.program.memory
        sig = (cur_kind, rt.cpu.s.cs, rt.cpu.s.ip & 0xFF00, m.ega_pan_active,
               bool(self.menu_page), self.menu_active)
        if self.gap_seen != sig:
            self.gap_seen = sig
            ctx = (f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X} mode={rt.dos.video_mode & 0xFF:02X}h "
                   f"pan_active={m.ega_pan_active} disp_en={m.ega_display_enabled} "
                   f"menu_active={self.menu_active} menu_seeded={bool(self.menu_page)} "
                   f"carte_fresh={self.carte_capture is not None and rt.cpu.instruction_count - self.carte_capture[2] < 60000}")
            print(f"[faithful] {FaithfulVisualGap(cur_kind)}\n            context: {ctx}", flush=True)
            if os.environ.get("PRE2_GAP_DUMP"):
                try:
                    from PIL import Image as _Img
                    vm = render_planar_rgb(mem_bytes, ds, rt.dos.vga_palette, 0,
                                           (m.ega_h_display_end + 1) * 8)
                    fn = f"artifacts/gap_{cur_kind.name}_{rt.cpu.s.ip:04x}_{rt.cpu.instruction_count}.png"
                    _Img.fromarray(vm).save(fn)
                    print(f"            [gap-dump] wrote {fn} (the VM screen at this gap)", flush=True)
                except Exception as _e:
                    print(f"            [gap-dump] failed: {_e}", flush=True)
        self.faithful_info = f"FAITHFUL GAP: {cur_kind.name} (see console)"
        return np.full((200, 320, 3), (48, 0, 32), dtype=np.uint8)
