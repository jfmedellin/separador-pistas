# Architecture

This document covers stem profiles, the Metal / Metal Stereo compliance boundary, and the module layout. For the end-user manual, see [MANUAL.md](MANUAL.md) (Spanish). For source setup, tests, and releases, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Stem profiles

A profile is the layout one separation publishes: which lanes exist, in which order, and which model produces them.

| Profile | Lanes | Status |
| --- | --- | --- |
| Legacy | vocals, drums, bass, other | Available. This is the default. |
| Metal Stereo | vocals, drums, bass, guitar center, guitar sides, other | Available. Splits the isolated guitar by stereo position. |
| Metal Roles | vocals, drums, bass, lead guitar, rhythm guitar, other | **Not available.** |

### Metal Stereo
<a id="metal-stereo"></a>

Metal Stereo isolates the guitar with `htdemucs_6s`, then splits that one stem into the part that sits in the centre of the stereo image and the part that is panned to the sides. Selecting it downloads a second set of model weights the first time, because `htdemucs_6s` is not the model the Legacy profile uses.

**It reads stereo position, not musical role.** Conventional metal production doubles the rhythm guitars and pans them wide while solos and melodies sit centred, so position happens to correlate with role often enough to be useful: muting `Guitar Sides` usually leaves the solo audible. That correlation is the whole benefit, and it is not a guarantee. A centred rhythm part lands in the centre lane, a wide harmonised lead lands in the sides lane, and nothing in this profile can tell the difference. Anything else centred in the mix that survives into the guitar stem lands in the centre lane too.

This is why the lanes are named for the position they describe and never for a role. Metal Stereo is not, and must not be presented as, lead and rhythm separation.

The split itself is arithmetic, not inference: centre plus sides reconstructs the isolated guitar. It is exact in float32, and the published lanes reconstruct to about 90 dB because a result inherits the 16-bit sample format Demucs wrote, which is a quantisation floor rather than a loss in the split. There are no weights to admit and no licence to satisfy, which is why this profile can ship enabled while the Metal Roles profile below cannot.

### Metal Roles
<a id="metal"></a>

**Metal Roles cannot separate lead from rhythm guitar today, and this release does not do it.** The profile exists so the surrounding infrastructure is in place, and it is shown in the app as unavailable with the reason, rather than hidden.

The reason is that no separation model exists that decomposes guitar into semantic lead and rhythm roles while also being redistributable and runnable offline on Windows. Every publicly available candidate was reviewed and rejected; `Compliance/evidence/metal-guitar/CONTRACT.md` records the rejection matrix and the contract any future model must satisfy.

Metal Roles cannot be turned on by editing a flag. A profile that requires a specialist and has none registered cannot be constructed in the enabled state, and readiness, separation, and the GUI each refuse it independently. Enabling it requires admitting a model through `Tools/admit_metal_guitar_model.py`, which is deny-by-default and reports `DENIED` in the shipped build:

```powershell
.\.venv\Scripts\python.exe .\Tools\admit_metal_guitar_model.py --offline
```

### Absent lanes

Lead guitar is intermittent by nature: many metal tracks have no solo at all. A profile with role lanes therefore treats a silent lane as a valid result whenever the role lanes still reconstruct the isolated guitar, and records the reason in the result manifest. A silent lane that loses energy is a failure and publishes nothing. An absent lane is published as real aligned silence and shown labeled in the mixer, never hidden.

## Module layout

```text
SeparationWorker/
|-- cli.py                    # Command-line entry point
|-- gui.py                    # Split and Mixer views
|-- demucs_worker.py          # Frozen worker entry point for Demucs
|-- demucs_adapter.py         # Profile-aware separation and publication
|-- guitar_adapter.py         # Fail-closed Lead/Rhythm specialist boundary
|-- gui_controller.py         # Background separation workflow
|-- mixer_controller.py       # Async mixer state and command boundary
`-- engine/
    |-- stem_profile.py       # Profile registry and result manifest
    |-- role_metrics.py       # Absence, audibility, and reconstruction limits
    |-- stem_cache.py         # App-managed temporary stem workspace
    |-- stem_session.py       # Published-layout validation and waveform peaks
    |-- stereo_split.py       # Deterministic centre/sides split of one stem
    |-- playback.py           # Shared-cursor Windows playback
    `-- mixer.py              # Immutable gain, Mute, and Solo semantics

Compliance/
|-- registry.py               # Profile-scoped, fail-closed asset readiness
`-- evidence/metal-guitar/    # Admission contract and uncalibrated thresholds

Tools/
`-- admit_metal_guitar_model.py  # Offline, deny-by-default admission harness
```

Admission and the runtime pipeline measure role decomposition with the same functions in `engine/role_metrics.py`. Separate copies would let a model pass one definition of "absent" and fail the other, which would make the admission gate meaningless.

The GUI never performs Demucs inference, WAV analysis, or native audio writes on the Tkinter event thread. In development, the adapter runs `python -m demucs.separate`. In the portable bundle, it launches the sibling `StemslayerWorker.exe`, which is the only process that imports and runs Demucs. Publication is atomic, so an incomplete result directory is never exposed.

## MVP boundaries

This release intentionally excludes macOS support, panning, mixed-WAV export, waveform zoom, output-device selection, and persisted mixer settings. It does **not** separate lead from rhythm guitar: the Metal profile ships disabled and no model is admitted, and Metal Stereo splits by stereo position rather than by role. Demucs uses `htdemucs` for the Legacy profile and `htdemucs_6s` for Metal Stereo. The CUDA portable automatically uses NVIDIA acceleration when available and retries once on CPU if CUDA inference fails; the CPU portable always uses CPU inference.

Every claim here is verified on Windows only. No macOS, commercial-use, or multi-player claim is made or implied.

---

See [../README.md](../README.md) for the project overview and download instructions, [MANUAL.md](MANUAL.md) for the end-user manual (Spanish), and [DEVELOPMENT.md](DEVELOPMENT.md) for source setup, tests, and releases.
