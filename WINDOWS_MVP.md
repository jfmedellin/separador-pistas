# Run the Stemslayer Windows MVP

Stemslayer separates one audio file into `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`, then opens those stems on one synchronized mixer timeline.

## Set up the environment

From the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
```

The script creates `.venv` and installs the pinned Windows audio, Demucs, GUI, and native drag-and-drop dependencies.

## Launch the desktop app

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.gui
```

Drag an audio file onto the Split view or select **Browse file**, then select **Separate into 4 stems**. The app writes the working result to its temporary cache and opens the mixer automatically. No result folder is required before separation.

In the mixer you can:

- play or pause all four stems on one shared timeline;
- click or drag the timeline to seek;
- adjust each stem from 0% through 100%;
- mute or solo individual stems; and
- export any selected stems to a destination folder.

Select **Load stems folder** to open an existing folder that already contains all four required WAV files.

## Run the command-line separator

The CLI still accepts an explicit destination directory:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli "C:\Music\song.mp3" "C:\Music\song-stems"
```

## Runtime behavior

The adapter uses the `htdemucs` model. It reuses a complete cached result for the same source path, selects CUDA when PyTorch reports an available GPU, and retries the complete separation once on CPU if the CUDA run fails. Publication is atomic, so an incomplete result directory is never exposed.

The first real run may download model weights. Fully offline packaging is outside this MVP. The mixer does not currently include panning, mixed-WAV export, looping, waveform zoom, output-device selection, or persisted settings.
