# Stemslayer

Stemslayer is a Windows-first desktop app for separating one song into four Demucs stems—**vocals**, **drums**, **bass**, and **other**—then auditioning and exporting them from a synchronized local mixer.

## Quick start

1. Open Windows PowerShell in the repository root.
2. Create the pinned environment:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Tools\setup_windows.ps1
   ```

3. Launch Stemslayer:

   ```powershell
   .\.venv\Scripts\python.exe -m SeparationWorker.gui
   ```

4. Drag an audio file onto the Split view, or select **Browse file**.
5. Select **Separate into 4 stems**. Stemslayer opens the mixer when the four WAV files are ready.
6. Select **Export** to copy any finished stems to a folder you choose.

The GUI stores working stems in an app-managed temporary cache. Selecting the same source again can reuse a complete cached result instead of running Demucs again. You do not need to choose an output folder before separation.

## Command line

The CLI accepts an explicit result directory and publishes exactly `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`:

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
| Volume | Attenuates one stem from 100% to 0%; positive boost is not exposed. |
| M | Mutes the selected stem. Mute takes priority over Solo. |
| S | Solos a stem; multiple stems can be soloed together. |
| Export | Copies the selected stems to a destination folder. |

You can also select **Load stems folder** to open an existing folder containing all four required WAV files. Playback uses one Windows audio stream and four synchronized readers so the stems cannot drift apart. Waveform peak envelopes are calculated in the background and bounded to 2,000 bins to keep long songs responsive.

## Architecture

```text
SeparationWorker/
|-- cli.py                    # Command-line entry point
|-- gui.py                    # Split and Mixer views
|-- gui_controller.py         # Background separation workflow
|-- mixer_controller.py       # Async mixer state and command boundary
`-- engine/
    |-- demucs_adapter.py     # Demucs execution and diagnostics
    |-- stem_cache.py         # App-managed temporary stem workspace
    |-- stem_session.py       # Four-stem validation and waveform peaks
    |-- playback.py           # Shared-cursor Windows playback
    `-- mixer.py              # Immutable gain, Mute, and Solo semantics
```

The GUI never performs Demucs inference, WAV analysis, or native audio writes on the Tkinter event thread. Playback failures release all readers and the output stream, then surface an actionable message.

## Testing

Run the portable suite with the project environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v
.\.venv\Scripts\python.exe -m compileall -q SeparationWorker Tests\Portable
```

The tests cover publication safety, Demucs diagnostics, cache lifecycle, four-stem metadata validation, synchronized playback with fake devices, controller threading, drag-and-drop payload parsing, and headless mixer view state.

## MVP boundaries

This release intentionally excludes macOS support, panning, mixed-WAV export, looping, waveform zoom, output-device selection, and persisted mixer settings. Demucs uses `htdemucs`; CUDA is selected when available, and the complete separation retries once on CPU when the CUDA run fails.

## Requirements

- Windows 10 or 11
- Python available as `python.exe`
- NVIDIA CUDA-capable GPU recommended for practical separation speed
- A Windows output device for mixer playback

`Tools/setup_windows.ps1` installs the pinned PyTorch, Demucs, SoundFile, SoundDevice, CustomTkinter, and tkinterdnd2 dependencies into `.venv`.