# Stemslayer

Stemslayer is a Windows-first desktop app for separating one song into four Demucs stems—**vocals**, **drums**, **bass**, and **other**—then auditioning and exporting them from a synchronized local mixer.

## Download the portable Windows release

The recommended distribution is the **Windows x64 portable ZIP** published in [GitHub Releases](https://github.com/jfmedellin/separador-pistas/releases). Do not use **Code → Download ZIP**; that downloads source code and still requires Python and the development dependencies.

1. Choose the portable ZIP for your computer and download its matching `.sha256` checksum from Releases:
   - `Stemslayer-vX.Y.Z-windows-x64-cuda-portable.zip` — recommended for supported NVIDIA GPUs.
   - `Stemslayer-vX.Y.Z-windows-x64-cpu-portable.zip` — universal fallback for computers without a supported NVIDIA GPU.
2. Verify the checksum if desired, then extract the ZIP to a folder you can write to.
3. Run `Stemslayer.exe` from the extracted folder.

Both portable bundles contain the GUI and their internal `StemslayerWorker.exe`; neither requires Python, Git, or a separate audio/Demucs installation. The CUDA build includes the CUDA runtime but requires a compatible NVIDIA GPU and current NVIDIA driver. The CPU build requires no NVIDIA hardware. The first separation downloads the `htdemucs` model weights. Later runs reuse the local model cache and reuse complete results for the same source when available. Internet access is required only for that first model download.

Use the CUDA build when possible: Demucs inference is substantially faster on a supported NVIDIA GPU. The tradeoff is download size—the CUDA ZIP is roughly 2 GB because it carries the NVIDIA runtime, while the CPU ZIP is roughly 210 MB. The CPU build is the compatibility option and can take several minutes per song. On machines with at least eight logical processors, the CPU worker uses two coordinated chunk workers and 10% overlap to reduce separation time without changing the four-stem model. The lower overlap is a balanced performance tradeoff and can slightly reduce quality at chunk boundaries compared with Demucs' 25% default.

## Development setup

For contributors who want to run from source:

1. Open Windows PowerShell in the repository root.
2. Create the pinned development environment:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
   ```

3. Launch Stemslayer:

   ```powershell
   .\.venv\Scripts\python.exe -m SeparationWorker.gui
   ```

4. Drag an audio file onto the Split view, or select **Browse file**.
5. Select **Separate into 4 stems**. The button names the count the selected profile publishes, and Stemslayer opens the mixer when those WAV files are ready.
6. Select **Export** to copy any finished stems to a folder you choose.

The source GUI stores working stems in an app-managed temporary cache. You do not need to choose an output folder before separation.

## Command line

The source CLI accepts an explicit result directory and publishes exactly `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli `
  "C:\Music\song.mp3" `
  "C:\Music\song-stems"
```

## Stem profiles

A profile is the layout one separation publishes: which lanes exist, in which order, and which model produces them.

| Profile | Lanes | Status |
| --- | --- | --- |
| Legacy | vocals, drums, bass, other | Available. This is the default. |
| Metal Stereo | vocals, drums, bass, guitar center, guitar sides, other | Available. Splits the isolated guitar by stereo position. |
| Metal | vocals, drums, bass, lead guitar, rhythm guitar, other | **Not available.** |

### Metal Stereo

Metal Stereo isolates the guitar with `htdemucs_6s`, then splits that one stem into the part that sits in the centre of the stereo image and the part that is panned to the sides. Selecting it downloads a second set of model weights the first time, because `htdemucs_6s` is not the model the Legacy profile uses.

**It reads stereo position, not musical role.** Conventional metal production doubles the rhythm guitars and pans them wide while solos and melodies sit centred, so position happens to correlate with role often enough to be useful: muting `Guitar Sides` usually leaves the solo audible. That correlation is the whole benefit, and it is not a guarantee. A centred rhythm part lands in the centre lane, a wide harmonised lead lands in the sides lane, and nothing in this profile can tell the difference. Anything else centred in the mix that survives into the guitar stem lands in the centre lane too.

This is why the lanes are named for the position they describe and never for a role. Metal Stereo is not, and must not be presented as, lead and rhythm separation.

The split itself is exact: centre plus sides reconstructs the isolated guitar, and there are no weights to admit and no licence to satisfy, which is why it can ship enabled while the Metal profile below cannot.

### Metal

**Metal cannot separate lead from rhythm guitar today, and this release does not do it.** The profile exists so the surrounding infrastructure is in place, and it is shown in the app as unavailable with the reason, rather than hidden.

The reason is that no separation model exists that decomposes guitar into semantic lead and rhythm roles while also being redistributable and runnable offline on Windows. Every publicly available candidate was reviewed and rejected; `Compliance/evidence/metal-guitar/CONTRACT.md` records the rejection matrix and the contract any future model must satisfy.

Metal cannot be turned on by editing a flag. A profile that requires a specialist and has none registered cannot be constructed in the enabled state, and readiness, separation, and the GUI each refuse it independently. Enabling it requires admitting a model through `Tools/admit_metal_guitar_model.py`, which is deny-by-default and reports `DENIED` in the shipped build:

```powershell
.\.venv\Scripts\python.exe .\Tools\admit_metal_guitar_model.py --offline
```

### Absent lanes

Lead guitar is intermittent by nature: many metal tracks have no solo at all. A profile with role lanes therefore treats a silent lane as a valid result whenever the role lanes still reconstruct the isolated guitar, and records the reason in the result manifest. A silent lane that loses energy is a failure and publishes nothing. An absent lane is published as real aligned silence and shown labeled in the mixer, never hidden.

## Mixer controls

| Control | Behavior |
| --- | --- |
| Play / Pause | Starts or pauses every published stem on one shared timeline. |
| Skip back / forward | Seeks 10 seconds against the current position, clamped to the track. |
| Loop | Restarts the track at the end instead of stopping, without reopening the output device. |
| Timeline | Click or drag to preview and seek to a frame. The playhead crosses every lane as one line. |
| Master volume | Attenuates the finished mix. It rides in front of the output clip, so it cannot lift a hot lane sum back over full scale. |
| Volume | Attenuates one stem from 100% to 0%; positive boost is not exposed. |
| M | Mutes the selected stem. Mute takes priority over Solo. |
| S | Solos a stem; multiple stems can be soloed together. |
| Export | Copies the selected stems to a destination folder. |

You can also select **Load stems folder** to open an existing folder containing the WAV files of a published result. Playback uses one Windows audio stream and one synchronized reader per published lane so the stems cannot drift apart. Waveform peak envelopes are calculated in the background and bounded to 2,000 bins to keep long songs responsive.

## Architecture

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

## Build the portable release

The release workflow builds and publishes both Windows x64 variants when a `vX.Y.Z` tag is pushed. To build the CPU variant locally:

```powershell
python -m venv .venv-portable
.\.venv-portable\Scripts\python.exe -m pip install --upgrade pip
.\.venv-portable\Scripts\python.exe -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv-portable\Scripts\python.exe -m pip install -r .\Tools\requirements-portable.txt
.\Tools\build_portable.ps1 -Version 1.1.2 -Variant cpu -PythonPath (Resolve-Path .\.venv-portable\Scripts\python.exe)
```

For the NVIDIA CUDA variant, install PyTorch from the CUDA 13.0 index instead and select the CUDA build contract:

```powershell
python -m venv .venv-portable-cuda
.\.venv-portable-cuda\Scripts\python.exe -m pip install --upgrade pip
.\.venv-portable-cuda\Scripts\python.exe -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cu130
.\.venv-portable-cuda\Scripts\python.exe -m pip install -r .\Tools\requirements-portable.txt
.\Tools\build_portable.ps1 -Version 1.1.2 -Variant cuda -PythonPath (Resolve-Path .\.venv-portable-cuda\Scripts\python.exe)
```

The script rejects a PyTorch runtime that does not match the requested variant, cleans `build/` and `dist/`, builds the one-folder GUI and worker, runs frozen entrypoint smoke tests, creates the variant-specific ZIP, and writes the matching `.sha256` file. Both ZIPs deliberately exclude model weights; `htdemucs` is acquired on first use.

## Testing

Run the portable suite with the project environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v
.\.venv\Scripts\python.exe -m unittest Tests.Admission.test_metal_guitar_admission -v
.\.venv\Scripts\python.exe -m compileall -q SeparationWorker Tests\Portable
```

The portable tests cover publication safety, Demucs diagnostics and command selection, cache lifecycle and pipeline namespacing, profile and manifest contracts, published-layout metadata validation, role reconstruction and absence, synchronized playback with fake devices, controller threading, drag-and-drop payload parsing, deterministic stereo-position splitting, looping and master gain, headless mixer view state, and real-widget lane rendering that measures every control stays reachable.

The admission suite asserts that the shipped build denies Metal. It is expected to report `DENIED`; that is the correct result while no specialist is admitted.

## MVP boundaries

This release intentionally excludes macOS support, panning, mixed-WAV export, waveform zoom, output-device selection, and persisted mixer settings. It does **not** separate lead from rhythm guitar: the Metal profile ships disabled and no model is admitted, and Metal Stereo splits by stereo position rather than by role. Demucs uses `htdemucs` for the Legacy profile and `htdemucs_6s` for Metal Stereo. The CUDA portable automatically uses NVIDIA acceleration when available and retries once on CPU if CUDA inference fails; the CPU portable always uses CPU inference.

Every claim here is verified on Windows only. No macOS, commercial-use, or multi-player claim is made or implied.

## Development requirements

- Windows 10 or 11
- Python 3.14 for source development and portable builds
- A Windows output device for mixer playback

`Tools/setup_windows.ps1` installs the pinned Windows development audio, Demucs, GUI, and native drag-and-drop dependencies into `.venv`. `Tools/requirements-portable.txt` contains the non-PyTorch portable dependencies; the release workflow installs the CPU and CUDA PyTorch wheels from their matching official indexes.
