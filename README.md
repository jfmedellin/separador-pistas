# Limbus Split

Windows-first stem separation and auditioning for four Demucs channels: **vocals**, **drums**, **bass**, and **other**.

The MVP turns one audio file into four aligned WAV files, then opens a local mixer where you can inspect waveforms, play or pause, seek, adjust volume, mute, and solo stems without leaving the app.

## Quick path

1. Use Windows PowerShell from the repository root.
2. Create the pinned environment:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
   ```

3. Launch the graphical interface:

   ```powershell
   .\.venv\Scripts\python.exe -m SeparationWorker.gui
   ```

4. Choose an audio file and an output location, then select **Separate into 4 stems**.
5. The mixer opens automatically after a successful separation. You can also load an existing folder containing the four required WAV files.

If the selected result folder already contains the complete four-stem output,
the app reopens that result without running Demucs again. To create a new
separation, choose a different result folder.

## Command line

The CLI writes a new result directory containing exactly `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli `
  "C:\Music\song.mp3" `
  "C:\Music\song-stems"
```

## Mixer controls

| Control | Behavior |
| --- | --- |
| Play / Pause | Starts or pauses all four stems on one shared timeline. |
| Timeline | Click or drag to preview and seek to a frame. |
| Volume | Attenuation from 0% through 100%; positive boost is not exposed. |
| M | Mute the selected stem. Mute takes priority over Solo. |
| S | Solo a stem; multiple stems can be soloed together. |

Playback uses one Windows audio stream and four synchronized readers so stems cannot drift apart. Waveform peak envelopes are calculated in the background and bounded to 2,000 bins, keeping long songs responsive.

## Architecture

```text
SeparationWorker/
├── cli.py                    # Command-line entry point
├── gui.py                    # Tkinter Separation and Mixer views
├── gui_controller.py         # Background separation workflow
├── mixer_controller.py       # Async mixer state and command boundary
└── engine/
    ├── demucs_adapter.py     # Demucs execution and diagnostics
    ├── stem_session.py       # Four-stem validation and waveform peaks
    ├── playback.py           # Shared-cursor Windows playback
    └── mixer.py              # Immutable gain, Mute, Solo semantics
```

The GUI never performs Demucs inference, WAV analysis, or native audio writes on the Tkinter event thread. Playback failures release all readers and the output stream, then surface an actionable message.

## Testing

Run the portable suite with the project environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v
.\.venv\Scripts\python.exe -m compileall -q SeparationWorker Tests\Portable
```

The tests cover publication safety, Demucs diagnostics, four-stem metadata validation, synchronized playback with fake devices, controller threading, and headless mixer view state.

## MVP boundaries

This release intentionally excludes macOS support, panning, mixed-WAV export, looping, waveform zoom, output-device selection, and persisted mixer settings. Demucs uses `htdemucs`; CUDA is selected when available and the complete separation retries once on CPU when the CUDA run fails.

## Requirements

- Windows 10/11
- Python available as `python.exe`
- NVIDIA CUDA-capable GPU recommended for practical separation speed
- A Windows output device for mixer playback

`Tools/setup_windows.ps1` installs PyTorch 2.13.0 with CUDA 13.0 support, Demucs 4.1.0, SoundFile 0.13.1, and SoundDevice 0.5.6 into `.venv`.
