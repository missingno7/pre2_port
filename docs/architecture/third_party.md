# Third-party code

`nuked_opl3` is vendored as an optional OPL/AdLib backend.  It must remain reusable and must not import `dos_re` or `pre2`.

The VM itself remains stdlib-only.  Viewer/audio dependencies are optional extras in `pyproject.toml`.
