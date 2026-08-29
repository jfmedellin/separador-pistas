# Run the Stemslayer Windows MVP

Stemslayer separates one audio file into `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`, then opens those stems on one synchronized mixer timeline.

## Recommended distribution: portable ZIP

Download the Windows x64 portable ZIP from [GitHub Releases](https://github.com/jfmedellin/separador-pistas/releases), not **Code → Download ZIP**. Extract it and run `Stemslayer.exe`. The extracted bundle includes the GUI and `StemslayerWorker.exe`, so users do not need Python, Git, NVIDIA drivers, or a separate Demucs installation.

The first separation downloads the `htdemucs` model weights. Later runs reuse the model cache and complete results when available. This first build is CPU-safe; CPU separation can take several minutes.

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

In source development, the adapter runs `python -m demucs.separate`, selects CUDA when PyTorch reports an available GPU, and retries the complete separation once on CPU if the CUDA run fails. In the portable bundle, the adapter launches the sibling `StemslayerWorker.exe`; that worker contains the CPU-only PyTorch and runs `demucs.separate` without a Python installation. Publication is atomic, so an incomplete result directory is never exposed.

The mixer does not currently include panning, mixed-WAV export, looping, waveform zoom, output-device selection, or persisted settings.
