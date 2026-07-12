# pre2/bridge/ — the DETACHABLE verification workbench

The VM-facing side of verification: what plugs the recovered/native game into the
`dos_re` oracle when a proof needs to run — and ONLY then. The shipped product
carries none of it (`scripts/deploy_native.py` denies the package; `scripts/lint.py`
forbids any shipped layer from importing it).

| Module | Role |
|---|---|
| `timing_fastforward.py` | Collapse classified VGA-retrace/PIT busy-waits in closed form (byte-equivalent, ~6-15x) for VM replay/record |
| `song_load_fastforward.py` | Same for the song-load loops |
| `frame_capture.py` | Capture rendered frames/state from the running VM |
| `audio_system.py` | VM audio-driver observation glue (hooks) |
| `object_interaction.py`, `objects.py`, `text.py`, `present.py` | VM-side observation/readers used by probes and verifiers |

The product's state views (human-named fields over the DGROUP layout, dataclass
readers) live in `pre2/views/` — that layer ships; this one detaches.
