# Stemslayer

> 📖 **¿Hablas español?** Lee el [Manual de usuario](docs/MANUAL.md) — instalación, ventanas de la app y qué versión descargar.

Stemslayer is a Windows-first desktop app that separates one song into stems with Demucs, then lets you audition and export them from a synchronized local mixer. Everything runs on your machine.

What you get depends on the **profile** you pick:

- **Legacy** (default) publishes four stems: **vocals**, **drums**, **bass**, **other**.
- **Metal Stereo** publishes six: the same four plus the guitar split into **Guitar Center** and **Guitar Sides**.

### Read this before you expect lead and rhythm guitar

**Stemslayer does not separate lead guitar from rhythm guitar. No profile here does, and none claims to.**

Metal Stereo splits the isolated guitar by **where it sits in the stereo image**, not by what it is playing. Metal is usually mixed with the rhythm guitars doubled and panned wide while solos sit centred, so muting `Guitar Sides` usually leaves the solo audible. That is the whole point of the profile, and it works because of how the music was mixed, not because anything recognised a solo.

So it fails in exactly the ways you would expect it to. A rhythm part recorded centred lands in the centre lane. A lead harmonised wide lands in the sides lane. Anything else centred in the mix that survives into the guitar stem — and in a dense metal mix there is always some — lands in the centre lane next to the solo.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#metal-stereo) for the full profile breakdown, including why the disabled **Metal Roles** (lead/rhythm) profile ships anyway.

## Download the portable Windows release

The recommended distribution is the **Windows x64 portable ZIP** published in [GitHub Releases](https://github.com/jfmedellin/separador-pistas/releases). Do not use **Code → Download ZIP**; that downloads source code and still requires Python and the development dependencies.

1. Choose the portable ZIP for your computer and download its matching `.sha256` checksum from Releases:
   - `Stemslayer-vX.Y.Z-windows-x64-cuda-portable.zip` — recommended for supported NVIDIA GPUs.
   - `Stemslayer-vX.Y.Z-windows-x64-cpu-portable.zip` — universal fallback for computers without a supported NVIDIA GPU.
2. Verify the checksum if desired, then extract the ZIP to a folder you can write to.
3. Run `Stemslayer.exe` from the extracted folder.

Both portable bundles contain the GUI and their internal `StemslayerWorker.exe`; neither requires Python, Git, or a separate audio/Demucs installation. The first separation downloads the model weights the selected profile needs: `htdemucs` for Legacy, and `htdemucs_6s` for Metal Stereo. They are different models, so choosing Metal Stereo for the first time downloads a second set. Later runs reuse the local model cache and reuse complete results for the same source when available. Internet access is required only for those first downloads.

| Build | Best for | Download size | Speed | Requirements |
| --- | --- | --- | --- | --- |
| CUDA | Supported NVIDIA GPU | ~2 GB (bundles the CUDA runtime) | Substantially faster | Compatible NVIDIA GPU + current driver |
| CPU | Any Windows PC | ~210 MB | Several minutes per song | None |

Use the CUDA build when possible. The CPU build is the compatibility option and always works if you are unsure which to pick.

## Requirements

- Windows 10 or 11

## Documentation

- [docs/MANUAL.md](docs/MANUAL.md) — end-user manual in Spanish: which ZIP to download, installing the app, and every window and control explained with screenshots.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — source setup, command line, testing, and the portable build and release process.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — stem profiles, the Metal/Metal Stereo compliance boundary, and module layout.
