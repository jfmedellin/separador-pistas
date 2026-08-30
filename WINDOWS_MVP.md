# Run the Stemslayer Windows MVP

Stemslayer separates one audio file into `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`, then opens those stems on one synchronized mixer timeline.

## Recommended distribution: portable ZIP

Download a Windows x64 portable ZIP from [GitHub Releases](https://github.com/jfmedellin/separador-pistas/releases), not **Code → Download ZIP**. Choose the CUDA ZIP for a supported NVIDIA GPU and current NVIDIA driver; choose the CPU ZIP as the universal fallback. Extract it and run `Stemslayer.exe`. Both bundles include the GUI and `StemslayerWorker.exe`, so users do not need Python, Git, or a separate Demucs installation.

The first separation downloads the `htdemucs` model weights. Later runs reuse the model cache and complete results when available. CUDA inference is substantially faster on supported NVIDIA hardware, but its ZIP is roughly 2 GB because it bundles the NVIDIA runtime; CPU separation can take several minutes. The CPU worker enables two chunk workers on machines with at least eight logical processors, coordinates their PyTorch thread limits, and uses 10% overlap. Smaller machines remain serial to avoid increasing memory pressure. This balanced overlap can slightly reduce quality at chunk boundaries compared with Demucs' 25% default.

## Run from source

From the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
.\.venv\Scripts\python.exe -m SeparationWorker.gui
```

The source setup uses the pinned Windows audio, Demucs, GUI, and native drag-and-drop dependencies. Drag an audio file onto the Split view or select **Browse file**, then select **Separate into 4 stems**. The app writes the working result to its temporary cache and opens the mixer automatically.

In the mixer you can:

- play or pause all four stems on one shared timeline;
- click or drag the timeline to seek;
- adjust each stem from 0% through 100%;
- mute or solo individual stems; and
- export any selected stems to a destination folder.

Select **Load stems folder** to open a folder that already contains all four required WAV files.

## Run the command-line separator

The source CLI still accepts an explicit destination directory:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli "C:\Music\song.mp3" "C:\Music\song-stems"
```

## Runtime behavior

In source development, the adapter runs `python -m demucs.separate`, selects CUDA when PyTorch reports an available GPU, and retries the complete separation once on CPU if the CUDA run fails. In either portable bundle, the adapter launches the sibling `StemslayerWorker.exe` without requiring a Python installation. The CUDA worker selects NVIDIA acceleration when available and retains the CPU retry; the CPU worker always uses CPU inference. Publication is atomic, so an incomplete result directory is never exposed.

The mixer does not currently include panning, mixed-WAV export, looping, waveform zoom, output-device selection, or persisted settings.

## Guitar separation is not available

The app lists a **Metal** profile with separate lead guitar and rhythm guitar lanes. **It cannot run, and this build does not separate lead from rhythm guitar.** Selecting it shows why instead of hiding the profile.

No separation model exists that decomposes guitar into semantic lead and rhythm roles while also being redistributable and runnable offline on Windows. Every publicly reviewed candidate was rejected; the rejection matrix and the contract a future model must satisfy are recorded in `Compliance/evidence/metal-guitar/CONTRACT.md`.

To check the current state yourself:

```powershell
.\.venv\Scripts\python.exe .\Tools\admit_metal_guitar_model.py --offline
```

It reports `DENIED` and exits with code 1. That is the correct result for this build, not a failure to fix. The four-stem Legacy profile is unaffected and remains the default.

## What is verified

Everything documented here is verified on Windows 10 and 11 only. No macOS behavior, commercial-use right, or multi-player capability is claimed or implied.
