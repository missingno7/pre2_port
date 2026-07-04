"""Enhanced-presentation *technique library* (not wired into any runtime).

These modules were built for the retired ``--video enhanced`` path over the hybrid VM session. That live
wiring (``renderer.py`` + ``bridge/faithful_session.py``) has been REMOVED — the hybrid faithful/enhanced
render experiment is gone (the native game, ``play_native``, is the recovered renderer's real home). What
remains here is the substrate-agnostic technique library, kept as reference for a future enhancement layer
over the **native** VM-less game:

* ``compositor`` — RGB/RGBA object-aware compose (interpolate sprites between source frames)
* ``extract`` / ``frame_state`` — lift a source frame into an ``EnhancedFrameState`` (bg + sprite textures)
* ``native_background`` — native per-tile background renderer (no ``render_frame`` in the hot path)
* ``sprite_cache`` — palette-independent sprite-texture cache
* ``transitions`` / ``transition_controller`` — present-time iris / vfade / curtain projections
* ``present`` — viewport scroll interpolation

Each is exercised by its own tests (``tests/test_enhanced_*`` / ``test_native_background`` / ``test_sprite_cache``)
and verify-probes, so they stay import-valid until a native enhancement layer re-homes them. Nothing here runs
at game time; ``play_native`` ships the faithful renderer only.
"""
