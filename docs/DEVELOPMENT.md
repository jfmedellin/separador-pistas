# Development

This document is for contributors who want to run Stemslayer from source, test it, or build and release the portable bundles. For the end-user manual, see [MANUAL.md](MANUAL.md) (Spanish). For stem profiles and module layout, see [ARCHITECTURE.md](ARCHITECTURE.md).

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

The source CLI always uses the Legacy profile and accepts an explicit result directory, so it publishes exactly `vocals.wav`, `drums.wav`, `bass.wav`, and `other.wav`. It has no profile option; Metal Stereo is reached from the GUI:

```powershell
.\.venv\Scripts\python.exe -m SeparationWorker.cli `
  "C:\Music\song.mp3" `
  "C:\Music\song-stems"
```

## Testing

Run the portable suite with the project environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v
.\.venv\Scripts\python.exe -m unittest Tests.Admission.test_metal_guitar_admission -v
.\.venv\Scripts\python.exe -m compileall -q SeparationWorker Tests\Portable
```

The portable tests cover publication safety, Demucs diagnostics and command selection, cache lifecycle and pipeline namespacing, profile and manifest contracts, published-layout metadata validation, role reconstruction and absence, synchronized playback with fake devices, controller threading, drag-and-drop payload parsing, deterministic stereo-position splitting, looping and master gain, headless mixer view state, and real-widget lane rendering that measures every control stays reachable.

The admission suite asserts that the shipped build denies Metal. It is expected to report `DENIED`; that is the correct result while no specialist is admitted.

## Release the portable builds

**Merging to `master` publishes nothing.** The release workflow runs only when a `vX.Y.Z` tag is pushed, so a merge with no tag produces no ZIP and no Release:

```yaml
on:
  push:
    tags:
      - "v*.*.*"
```

Tag `master` once the change is merged and the tests pass there:

```powershell
git checkout master
git pull
git tag v1.2.0
git push origin v1.2.0
```

The workflow then reinstalls the pinned dependencies, runs the portable suite on a Windows runner, builds the CPU and CUDA bundles, and attaches both ZIPs with their `.sha256` files to a GitHub Release. It takes roughly fifteen minutes. If the suite fails on the runner, no Release is published; the tag stays and can be deleted and re-pushed after a fix.

### Build one variant locally

To build the CPU variant without the workflow:

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

The script rejects a PyTorch runtime that does not match the requested variant, cleans `build/` and `dist/`, builds the one-folder GUI and worker, runs frozen entrypoint smoke tests, creates the variant-specific ZIP, and writes the matching `.sha256` file. Both ZIPs deliberately exclude model weights; `htdemucs` and `htdemucs_6s` are acquired on first use of the profile that needs them.

## Development requirements

- Windows 10 or 11
- Python 3.14 for source development and portable builds
- A Windows output device for mixer playback

`Tools/setup_windows.ps1` installs the pinned Windows development audio, Demucs, GUI, and native drag-and-drop dependencies into `.venv`. `Tools/requirements-portable.txt` contains the non-PyTorch portable dependencies; the release workflow installs the CPU and CUDA PyTorch wheels from their matching official indexes.

---

See [../README.md](../README.md) for the project overview and download instructions, [MANUAL.md](MANUAL.md) for the end-user manual (Spanish), and [ARCHITECTURE.md](ARCHITECTURE.md) for stem profiles and module layout.
