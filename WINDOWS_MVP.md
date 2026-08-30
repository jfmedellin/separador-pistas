# Run the Stemslayer Windows MVP

Stemslayer separates one audio file into stems and opens them on one synchronized mixer timeline. The default Legacy profile publishes `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`; the Metal Stereo profile publishes six lanes and splits the guitar by stereo position.

## Recommended distribution: portable ZIP

Download a Windows x64 portable ZIP from [GitHub Releases](https://github.com/jfmedellin/separador-pistas/releases), not **Code → Download ZIP**. Choose the CUDA ZIP for a supported NVIDIA GPU and current NVIDIA driver; choose the CPU ZIP as the universal fallback. Extract it and run `Stemslayer.exe`. Both bundles include the GUI and `StemslayerWorker.exe`, so users do not need Python, Git, or a separate Demucs installation.

The first separation downloads the `htdemucs` model weights, and the first Metal Stereo separation downloads `htdemucs_6s` as well. Later runs reuse the model cache and complete results when available. CUDA inference is substantially faster on supported NVIDIA hardware, but its ZIP is roughly 2 GB because it bundles the NVIDIA runtime; CPU separation can take several minutes. The CPU worker enables two chunk workers on machines with at least eight logical processors, coordinates their PyTorch thread limits, and uses 10% overlap. Smaller machines remain serial to avoid increasing memory pressure. This balanced overlap can slightly reduce quality at chunk boundaries compared with Demucs' 25% default.

## Run from source

From the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
.\.venv\Scripts\python.exe -m SeparationWorker.gui
```

The source setup uses the pinned Windows audio, Demucs, GUI, and native drag-and-drop dependencies. Drag an audio file onto the Split view or select **Browse file**, then select **Separate into 4 stems**, which names the count the selected profile publishes. The app writes the working result to its temporary cache and opens the mixer automatically.

In the mixer you can:

- play or pause every published stem on one shared timeline;
- click or drag the timeline to seek, or skip 10 seconds back and forward;
- loop the track so it restarts instead of stopping;
- adjust each stem from 0% through 100%, and the finished mix with the master volume;
- mute or solo individual stems; and
- export any selected stems to a destination folder.

Select **Load stems folder** to open a folder that already contains the WAV files of a published result.

## Run the command-line separator

The source CLI still accepts an explicit destination directory:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli "C:\Music\song.mp3" "C:\Music\song-stems"
```

## Runtime behavior

In source development, the adapter runs `python -m demucs.separate`, selects CUDA when PyTorch reports an available GPU, and retries the complete separation once on CPU if the CUDA run fails. In either portable bundle, the adapter launches the sibling `StemslayerWorker.exe` without requiring a Python installation. The CUDA worker selects NVIDIA acceleration when available and retains the CPU retry; the CPU worker always uses CPU inference. Publication is atomic, so an incomplete result directory is never exposed.

The mixer does not currently include panning, mixed-WAV export, waveform zoom, output-device selection, or persisted settings.

## Guitar: what this build does and does not do

The app offers a **Metal Stereo** profile that runs, and a **Metal** profile that does not. They are not two versions of the same thing.

**Metal Stereo** isolates the guitar and then splits it by stereo position into `Guitar Center` and `Guitar Sides`. Because metal is usually mixed with doubled rhythm guitars panned wide and solos placed centred, muting the sides usually leaves the solo audible, which is what the profile is for. It reads position, not musical role: a centred rhythm part lands in the centre lane, a wide harmonised lead lands in the sides lane, and anything else centred in the mix that survives into the guitar stem lands in the centre lane with the solo. The lanes are named for position for exactly that reason.

**Metal**, with separate lead guitar and rhythm guitar lanes, **cannot run, and this build does not separate lead from rhythm guitar.** Selecting it shows why instead of hiding the profile.

No separation model exists that decomposes guitar into semantic lead and rhythm roles while also being redistributable and runnable offline on Windows. Every publicly reviewed candidate was rejected; the rejection matrix and the contract a future model must satisfy are recorded in `Compliance/evidence/metal-guitar/CONTRACT.md`.

To check the current state yourself:

```powershell
.\.venv\Scripts\python.exe .\Tools\admit_metal_guitar_model.py --offline
```

It reports `DENIED` and exits with code 1. That is the correct result for this build, not a failure to fix. It says nothing about Metal Stereo, which needs no admitted model: the Legacy and Metal Stereo profiles are unaffected, and Legacy remains the default.

## What is verified

Everything documented here is verified on Windows 10 and 11 only. No macOS behavior, commercial-use right, or multi-player capability is claimed or implied.
