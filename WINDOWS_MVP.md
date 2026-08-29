# Windows MVP separator

The first usable backend accepts one audio file and creates a new result
directory containing exactly `vocals.wav`, `drums.wav`, `bass.wav`, and
`other.wav`.

Create the pinned Windows environment from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
```

The setup installs PyTorch 2.13.0 from the official CUDA 13.0 wheel index,
Demucs 4.1.0, SoundFile 0.13.1, and SoundDevice 0.5.6 into `.venv`.

Run the separator with that environment's Python executable:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli "C:\Music\song.mp3" "C:\Music\song-stems"
```

Or launch the minimal Windows interface:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.gui
```

After a successful separation, the mixer opens automatically with the four
published WAV files. You can also select **Open stems folder** and choose an
existing folder containing `vocals.wav`, `drums.wav`, `bass.wav`, and
`other.wav`. The mixer shows one waveform lane per stem and a shared timeline.
Use **Play** or **Pause**, click or drag the timeline to seek, adjust each
volume from 0% through 100%, and use **M** (Mute) or **S** (Solo). Several
stems can be soloed together; mute always takes priority.

The mixer intentionally does not include panning, export, looping, zooming,
output-device selection, or persisted settings in this MVP. Playback uses one
Windows audio stream so all four stems keep the same frame position.

The adapter uses the `htdemucs` model. It reuses an already complete result
folder instead of running Demucs again, selects CUDA when PyTorch reports an
available GPU, and retries the complete separation once on CPU if the CUDA run
fails. Publication is atomic: an incomplete result directory is never exposed.

The first real run may download the model weights. Fully offline packaging is
outside this MVP work unit.
