# Windows MVP separator

The first usable backend accepts one audio file and creates a new result
directory containing exactly `vocals.wav`, `drums.wav`, `bass.wav`, and
`other.wav`.

Create the pinned Windows environment from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
```

The setup installs PyTorch 2.13.0 from the official CUDA 13.0 wheel index,
Demucs 4.1.0, and SoundFile 0.13.1 into `.venv`.

Run the separator with that environment's Python executable:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli "C:\Music\song.mp3" "C:\Music\song-stems"
```

Or launch the minimal Windows interface:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.gui
```

The adapter uses the `htdemucs` model. It selects CUDA when PyTorch reports an
available GPU and retries the complete separation once on CPU if the CUDA run
fails. Publication is atomic: an incomplete result directory is never exposed.

The first real run may download the model weights. Fully offline packaging is
outside this MVP work unit.
