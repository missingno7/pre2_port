"""pre2 adapter for dos_re.frontend_timeline: classify + render a front-end frame from BOTH sides to a common form.

A "front-end frame" reduces to ``(screen_id, rgb)`` where ``screen_id`` is a COARSE logical screen (robust to the
scroll pel-pan / fade palette — the sequence gate compares these) and ``rgb`` is the (200,320,3) RGB the pixel gate
digests. The two producers:

  * :func:`classify_vm_frame` — the reference VM's actual framebuffer (mode 13h linear / 0Dh planar / text), decoded
    exactly as the play.py viewer does (the CRTC display-start + pel-pan + active width), then the 13h image is
    fingerprinted against the game's own assets to name it (TITUS / PRESENT / MENU / CASTLE / ...).
  * :func:`classify_native_scene` — a VM-less :class:`FrontEndScene` from the native front-end generator, rendered by
    the SAME ``front_end_scene_to_rgb`` the runner presents, and named by fingerprinting its linear image the same way.

Both go through :func:`dos_re.frontend_timeline.rgb_sha` so the pixel digests are comparable. The 0Dh screens (oldies
credits / the scrolling mode-select+password map / the carte / gameplay HUD) are coarsely "0Dh" here; the 13h screens
(every title/menu/wall image) are named — which is what the cold-start intro->menu sequence is made of.
"""
from __future__ import annotations

from pre2.bridge.image_scene import _IMAGE_OFF, _fingerprint

# the mode-13h image assets the front end shows (title / menu / wall); fingerprinted by their first 256 image bytes
# (palette-independent, so a mid-fade frame still names correctly once the image bytes are copied in). THEEND.SQZ is
# deliberately EXCLUDED: it is black-topped, so its fingerprint is all-zero and would match every black fade-start
# frame (the same reason image_scene.identify_image skips a degenerate all-zero fingerprint).
_FP_ASSETS = ("PRESENT.SQZ", "MENU.SQZ", "MENU2.SQZ", "TITUS.SQZ", "MOTIF.SQZ", "CASTLE.SQZ")


def _fingerprint_map(game_root: str) -> dict:
    fp = {}
    for name in _FP_ASSETS:
        try:
            head = _fingerprint(game_root, name)
            if any(head):                       # skip a degenerate all-zero head (can't be told from a black frame)
                fp[head] = name
        except FileNotFoundError:
            continue
    return fp


def _name_13h(head256: bytes, fpmap: dict) -> str:
    head = bytes(head256)
    if not any(head):
        return "loading"                        # black head = fade start / image not yet copied (not a real screen)
    return fpmap.get(head, "?")


def classify_vm_frame(rt, game_root: str, fpmap: dict):
    """Classify + render the reference VM's current framebuffer. Returns ``(screen_id, rgb | None)``.

    Mirrors play.py's ``render_current`` (the viewer's faithful VM-framebuffer path) exactly so the pixels are the
    ones the original shows: text modes, 13h linear (VGA), 0Dh planar (EGA) with the live CRTC pan/width."""
    from sdl_view import render_planar_rgb, render_text_rgb, render_vga_rgb

    mem_o = rt.program.memory
    dos = rt.dos
    if not mem_o.ega_display_enabled:
        return "blanked", None                     # display off (palette load) — the original holds the last frame
    mem = bytes(mem_o.data)
    mode = dos.video_mode & 0x7F
    if mode in (0, 1, 2, 3, 7):
        return "text", render_text_rgb(mem, dos.video_mode & 0xFF, dos.video_page)
    if mode in (0x13, 0x19):
        rgb = render_vga_rgb(mem, dos.vga_palette)
        return f"13h:{_name_13h(mem[0xA0000:0xA0000 + 256], fpmap)}", rgb
    if mem_o.ega_planar:
        if mem_o.ega_pan_active:
            ds, pel = mem_o.ega_pan_display_start, mem_o.ega_pan_pel
        else:
            ds, pel = mem_o.ega_display_start, 0
        active_w = (mem_o.ega_h_display_end + 1) * 8   # CRTC active width (carte = 312, else 320)
        return "0Dh", render_planar_rgb(mem, ds, dos.vga_palette, pel, active_w)
    return "other", None


def classify_native_scene(scene, game_root: str, fpmap: dict):
    """Classify + render a VM-less :class:`FrontEndScene`. Returns ``(screen_id, rgb)`` — same id space + same
    renderer (``front_end_scene_to_rgb``) as :func:`classify_vm_frame`, so the two are directly comparable."""
    from sdl_view import front_end_scene_to_rgb

    rgb = front_end_scene_to_rgb(scene)
    if scene.mode == 0x13:                                            # MODE_LINEAR — name by the image head
        return f"13h:{_name_13h(bytes(scene.linear[:256]), fpmap)}", rgb
    if scene.mode == 0x12:                                            # MODE_CREATORS (640x480 12h)
        return "12h", rgb
    return "0Dh", rgb                                                 # MODE_PLANAR (oldies / map / carte)
