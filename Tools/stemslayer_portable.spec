# PyInstaller one-folder build for the Windows x64 portable release.
# The release environment installs the CPU-only PyTorch wheel before invoking
# this spec. No model weights are collected here: SEC-01's app-owned
# SeparationWorker.model_manager verifies and downloads them at first use
# into a per-model app-owned cache directory, never through Demucs' own
# on-use download or torch.hub (see SeparationWorker/model_manager.py).

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, copy_metadata


project_root = Path(SPECPATH).resolve().parent


def _unique(entries):
    seen = set()
    result = []
    for source, destination, *rest in entries:
        key = (str(source).lower(), str(destination).lower(), tuple(rest))
        if key not in seen:
            seen.add(key)
            result.append((source, destination, *rest))
    return result


def _source_path(entry):
    return str(entry[0]).replace("\\", "/").lower()


datas = []
binaries = []
hiddenimports = [
    "torch",
    "torch._C",
    "torch.cuda",
    "torch.nn",
    "torch.nn.functional",
    "torch.nn.modules",
    "torch.utils",
    "demucs.separate",
    # Demucs checkpoints pickle `numpy.core.multiarray.scalar`. Under numpy 2
    # `numpy.core` is only a compatibility shim that nothing imports
    # statically, so without this the frozen worker fails torch.load with
    # "No module named 'numpy.core.multiarray'" on first separation.
    "numpy.core.multiarray",
    "mutagen",
    "_soundfile_data",
    "_sounddevice_data",
]

# These packages contain runtime Python modules and/or data that static
# analysis cannot reliably discover (Demucs model definitions, Mutagen format
# handlers, CustomTkinter assets, and TkDND's Tcl scripts/native extension).
# `collect_all("demucs")` already sweeps every non-.py data file under the
# installed `demucs` package with no `excludes`, which includes
# `demucs/remote/*.yaml` (verified at apply time: both `htdemucs.yaml` and
# `htdemucs_6s.yaml` are present in the collected datas). SEC-01's
# `model_manager` copies these bundled bag descriptors from that same
# installed location (D7), so no separate `datas` entry is needed for them.
for package in ("demucs", "customtkinter", "mutagen"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

tkdnd_datas, tkdnd_binaries, tkdnd_hiddenimports = collect_all("tkinterdnd2")
datas.extend(
    entry
    for entry in tkdnd_datas
    if "/tkinterdnd2/tkdnd/win-x64/" in _source_path(entry)
)
binaries.extend(
    entry
    for entry in tkdnd_binaries
    if "/tkinterdnd2/tkdnd/win-x64/" in _source_path(entry)
)
hiddenimports.extend(tkdnd_hiddenimports)

# PyTorch's package contains a large C++ header tree that is not needed at
# runtime. Collect its native libraries and small runtime data instead of
# copying development headers into the portable ZIP.
binaries.extend(collect_dynamic_libs("torch"))
datas.extend(
    entry
    for entry in collect_data_files(
        "torch",
        includes=["*.json", "*.yaml", "*.yml"],
        excludes=["torch/include/*"],
    )
)

# soundfile and sounddevice are modules, while their bundled native assets
# live in these companion packages. Keep only the Windows x64 native files.
soundfile_datas, soundfile_binaries, soundfile_hiddenimports = collect_all("_soundfile_data")
datas.extend(
    entry
    for entry in soundfile_datas
    if _source_path(entry).endswith("/libsndfile_x64.dll")
)
binaries.extend(soundfile_binaries)
hiddenimports.extend(soundfile_hiddenimports)

sounddevice_datas, sounddevice_binaries, sounddevice_hiddenimports = collect_all("_sounddevice_data")
datas.extend(
    entry
    for entry in sounddevice_datas
    if _source_path(entry).endswith("/libportaudio64bit.dll")
)
binaries.extend(sounddevice_binaries)
hiddenimports.extend(sounddevice_hiddenimports)

for package in ("demucs", "torch", "customtkinter", "tkinterdnd2", "soundfile", "sounddevice", "mutagen"):
    datas.extend(copy_metadata(package))

datas = _unique(datas)
binaries = _unique(binaries)
hiddenimports = sorted(set(hiddenimports))


gui_analysis = Analysis(
    [str(project_root / "SeparationWorker" / "gui.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    name="Stemslayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

worker_analysis = Analysis(
    [str(project_root / "SeparationWorker" / "demucs_worker.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
worker_pyz = PYZ(worker_analysis.pure)
worker_exe = EXE(
    worker_pyz,
    worker_analysis.scripts,
    name="StemslayerWorker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

# Both executables share one onedir runtime directory. This keeps the ZIP
# portable and lets the GUI find StemslayerWorker.exe beside itself.
bundle = COLLECT(
    gui_exe,
    worker_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    gui_analysis.zipfiles,
    worker_analysis.binaries,
    worker_analysis.datas,
    worker_analysis.zipfiles,
    name="Stemslayer",
)
