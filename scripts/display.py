"""Window + present backend for play_native.

Prefers a GPU-accelerated SDL2 renderer (``pygame._sdl2.video``): the small game frame (320x200, or a bit
wider in widescreen) is uploaded once per frame as a streaming texture and the GPU scales it to the window.
So present cost is ~constant regardless of window size — a 4K / fullscreen window is as cheap as a tiny one
(the old software path scaled + flipped the whole WINDOW surface every frame, so fps fell as the window grew).

Falls back to the software ``pygame.display`` surface path when the SDL2 renderer is unavailable, so nothing
regresses on odd setups. Both paths share the aspect-correct letterbox math and an ``integer_scale`` option.
"""
from __future__ import annotations

import numpy as np
import pygame

_TITLE = "PRE2 — VM-less native"


class Display:
    def __init__(self, size, *, title: str = _TITLE, icon=None):
        self.integer_scale = False
        self.par = 1.0                     # displayed pixel aspect (height/width). 1.0 = square pixels;
        #                                    1.2 = the DOS 4:3 look (320x200 shown at 4:3 -> pixels 1.2x tall).
        self.gpu = False
        self._srcsurf = None
        self._texsize = None
        self._tex = None
        self._ov = {}                      # cached overlay textures keyed by id(surface)
        try:
            from pygame._sdl2 import video as sdl2
            self._sdl2 = sdl2
            self.window = sdl2.Window(title, size=size, resizable=True)
            if icon is not None:
                try:
                    self.window.set_icon(icon)          # SDL2 window's own icon (titlebar / taskbar)
                except Exception:                       # noqa: BLE001 — older pygame lacks Window.set_icon
                    pass
            self.renderer = sdl2.Renderer(self.window, accelerated=-1, vsync=False)
            self.renderer.draw_color = (0, 0, 0, 255)
            self.gpu = True
        except Exception:                  # noqa: BLE001 — no GPU / no _sdl2 -> software surface
            self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)

    # --- geometry -------------------------------------------------------------------------------------
    def get_size(self):
        return tuple(self.window.size) if self.gpu else self.screen.get_size()

    def letterbox(self, fw: int, fh: int) -> "pygame.Rect":
        """Aspect-correct destination rect for an fw×fh frame centred in the window (integer-snapped if set).
        ``par`` (pixel aspect, height/width) stretches the frame vertically so square-buffer content displays
        at the intended pixel shape: par=1.2 shows 320x200 at 4:3 (the DOS CRT look) instead of 1.6:1."""
        sw, sh = self.get_size()
        eh = fh * self.par                                   # effective (displayed) frame height in px units
        f = min(sw / fw, sh / eh)
        if self.integer_scale and f >= 1.0:
            f = float(int(f))
        tw, th = max(1, int(fw * f)), max(1, int(eh * f))
        return pygame.Rect((sw - tw) // 2, (sh - th) // 2, tw, th)

    # --- drawing --------------------------------------------------------------------------------------
    def draw_game(self, rgb) -> "pygame.Rect":
        """Draw one game frame (an H×W×3 uint8 array) scaled + letterboxed; returns its on-screen rect. Does
        NOT present — call flip() after any overlays."""
        arr = np.asarray(rgb, np.uint8)
        fh, fw = arr.shape[:2]
        rect = self.letterbox(fw, fh)
        if self.gpu:
            if self._texsize != (fw, fh):
                self._tex = self._sdl2.Texture(self.renderer, (fw, fh), streaming=True)
                self._srcsurf = pygame.Surface((fw, fh))
                self._texsize = (fw, fh)
            pygame.surfarray.blit_array(self._srcsurf, arr.swapaxes(0, 1))
            self._tex.update(self._srcsurf)
            self.renderer.clear()                                 # black letterbox bars
            self._tex.draw(dstrect=rect)
        else:
            if self._texsize != (fw, fh):
                self._srcsurf = pygame.Surface((fw, fh))
                self._texsize = (fw, fh)
            pygame.surfarray.blit_array(self._srcsurf, arr.swapaxes(0, 1))
            self.screen.fill((0, 0, 0))
            pygame.transform.scale(self._srcsurf, rect.size, self.screen.subsurface(rect))
        return rect

    def draw_overlay(self, surf, pos, *, slot=None, changed=True) -> None:
        """Composite a pygame Surface (fps readout / the F10 menu) on top at window pixel ``pos``, alpha-blended.
        A persistent streaming texture per size is re-uploaded each call (content changes every frame), so no
        per-frame GPU allocation."""
        if self.gpu:
            sz = surf.get_size()
            key = slot if slot is not None else sz               # dedicated slot (touch) vs shared-by-size (menu/fps)
            entry = self._ov.get(key)
            if entry is None or entry[1] != sz:                  # (re)create on first use or window resize
                if entry is None and len(self._ov) > 8:
                    self._ov.clear()
                tex = self._sdl2.Texture(self.renderer, sz, streaming=True)
                tex.blend_mode = 1                                # SDL_BLENDMODE_BLEND (alpha)
                self._ov[key] = entry = (tex, sz)
                changed = True                                    # a fresh texture must be uploaded once
            tex = entry[0]
            # ``changed=False`` reuses the last-uploaded texture — the touch overlay is a window-size (~18 MB)
            # surface whose per-frame re-upload was the entire mobile present cost; it only changes when a control
            # moves, so the interpolation sub-frames (and steady input) skip the upload. Menu/fps pass the default.
            if changed:
                tex.update(surf)
            tex.draw(dstrect=pygame.Rect(pos, sz))
        else:
            self.screen.blit(surf, pos)

    def make_sprite(self, surf):
        """A persistent drawable from a SMALL surface — a static GPU texture uploaded ONCE (or the surface on
        the software path). Returns ``(drawable, w, h)``. For HUD/controls that MOVE but don't change content:
        drawing them each frame is then a cheap quad, with no per-frame surface upload (unlike draw_overlay,
        which re-uploads a whole window-size surface every call — murder on hi-res mobile GPUs)."""
        w, h = surf.get_size()
        if self.gpu:
            tex = self._sdl2.Texture.from_surface(self.renderer, surf)
            tex.blend_mode = 1                                    # SDL_BLENDMODE_BLEND (alpha)
            return (tex, w, h)
        return (surf.convert_alpha(), w, h)

    def draw_sprite(self, spr, pos) -> None:
        """Draw a :meth:`make_sprite` drawable with its top-left at window-pixel ``pos``."""
        drawable, w, h = spr
        if self.gpu:
            drawable.draw(dstrect=pygame.Rect(int(pos[0]), int(pos[1]), w, h))
        else:
            self.screen.blit(drawable, (int(pos[0]), int(pos[1])))

    def new_overlay_canvas(self):
        """A transparent window-size surface to draw the modal F10 menu onto (then draw_overlay it)."""
        return pygame.Surface(self.get_size(), pygame.SRCALPHA)

    def flip(self) -> None:
        if self.gpu:
            self.renderer.present()
        else:
            pygame.display.flip()

    # --- window state ---------------------------------------------------------------------------------
    def resize(self, w: int, h: int) -> None:
        """Handle a user window drag-resize (software path re-creates the surface; GPU auto-tracks)."""
        if self.gpu:
            self.window.size = (max(160, w), max(100, h))
        else:
            self.screen = pygame.display.set_mode((max(160, w), max(100, h)), pygame.RESIZABLE)

    def set_fullscreen(self, on: bool, windowed_size=None) -> None:
        """Borderless fullscreen (SDL's own fullscreen-desktop on the GPU path — DPI/monitor correct, no ctypes;
        the software path recreates a NOFRAME desktop-sized window)."""
        if self.gpu:
            self.window.set_fullscreen(desktop=True) if on else self.window.set_windowed()
            if not on and windowed_size:
                self.window.size = windowed_size
            self._ov.clear()                                      # window size changed -> stale overlay textures
        else:
            import os
            if on:
                try:
                    dw, dh = pygame.display.get_desktop_sizes()[0]
                except Exception:                                # noqa: BLE001
                    info = pygame.display.Info(); dw, dh = info.current_w, info.current_h
                os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
                self.screen = pygame.display.set_mode((dw, dh), pygame.NOFRAME)
            else:
                os.environ.pop("SDL_VIDEO_WINDOW_POS", None)
                self.screen = pygame.display.set_mode(windowed_size or (1280, 800), pygame.RESIZABLE)
        self._texsize = None                                     # force src-surface rebuild against the new target
