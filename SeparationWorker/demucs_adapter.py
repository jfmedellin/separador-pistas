"""Small Demucs v4.1 adapter for the Windows four-stem MVP."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from SeparationWorker.engine.publication import publish_atomic
from SeparationWorker.engine.stem_session import STEM_NAMES


MODEL_NAME = "htdemucs"
WORKER_EXECUTABLE = "StemslayerWorker.exe"
DIAGNOSTIC_MAX_LINES = 6
DIAGNOSTIC_MAX_CHARACTERS = 800
CPU_JOBS = 2
CPU_JOBS_MIN_LOGICAL_PROCESSORS = 8
CPU_OVERLAP = 0.1


@dataclass(eq=False)
class DemucsSeparationError(RuntimeError):
    code: str
    cause: str
    recovery: str

    def __str__(self) -> str:
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


def cuda_is_available() -> bool:
    """Return false when PyTorch or a usable CUDA runtime is unavailable."""
    try:
        import torch
    except (ImportError, OSError):
        return False
    try:
        return bool(torch.cuda.is_available())
    except (OSError, RuntimeError):
        return False


def _require_demucs_41() -> None:
    try:
        version = importlib.metadata.version("demucs")
    except importlib.metadata.PackageNotFoundError as exc:
        if getattr(sys, "frozen", False):
            # PyInstaller bundles the package code, but metadata is optional.
            # The release spec still copies metadata; this fallback keeps the
            # frozen runtime useful with bootloaders that omit dist-info.
            try:
                import demucs

                version = demucs.__version__
            except (ImportError, AttributeError) as fallback_error:
                raise DemucsSeparationError(
                    "demucs.not_installed",
                    "The bundled Demucs runtime is not available.",
                    "Re-download the complete Stemslayer portable bundle and retry.",
                ) from fallback_error
        else:
            raise DemucsSeparationError(
                "demucs.not_installed",
                "Demucs is not installed in the active Python environment.",
                "Install the Windows MVP dependencies in its isolated environment and retry.",
            ) from exc
    if version.split(".")[:2] != ["4", "1"]:
        raise DemucsSeparationError(
            "demucs.version_mismatch",
            f"Demucs 4.1 is required, but version {version} is installed.",
            "Activate the Windows MVP environment containing Demucs 4.1 and retry.",
        )


def run_demucs(command: Sequence[str]) -> None:
    _require_demucs_41()
    options = {
        "check": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    environment = _cpu_process_environment(command)
    if environment is not None:
        options["env"] = environment
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        # The worker is a console executable so Demucs can be captured, but
        # it must not flash a console window over the GUI.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            options["creationflags"] = creationflags
    subprocess.run(
        list(command),
        **options,
    )


def _option_value(command: Sequence[str], option: str) -> str | None:
    try:
        index = command.index(option)
        return command[index + 1]
    except (ValueError, IndexError):
        return None


def _cpu_jobs(logical_processors: int | None = None) -> int:
    if logical_processors is None:
        logical_processors = os.cpu_count() or 1
    return CPU_JOBS if logical_processors >= CPU_JOBS_MIN_LOGICAL_PROCESSORS else 0


def _cpu_thread_limit(jobs: int) -> int:
    try:
        import torch

        available_threads = int(torch.get_num_threads())
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        available_threads = max(1, (os.cpu_count() or 1) // 2)
    return max(1, available_threads // max(1, jobs))


def _cpu_process_environment(command: Sequence[str]) -> dict[str, str] | None:
    if _option_value(command, "--device") != "cpu":
        return None
    try:
        jobs = int(_option_value(command, "--jobs") or "0")
    except ValueError:
        return None
    if jobs <= 0:
        return None
    threads = str(_cpu_thread_limit(jobs))
    environment = os.environ.copy()
    environment["OMP_NUM_THREADS"] = threads
    environment["MKL_NUM_THREADS"] = threads
    return environment


def frozen_worker_path() -> Path:
    """Return the worker beside the GUI executable in a frozen bundle."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("The frozen worker path is only available in a bundled runtime.")
    worker = Path(sys.executable).resolve().with_name(WORKER_EXECUTABLE)
    if not worker.is_file():
        raise DemucsSeparationError(
            "demucs.worker_missing",
            f"The bundled Demucs worker is missing: {worker}",
            "Extract the complete Stemslayer portable folder and retry.",
        )
    return worker


def _diagnostic_tail(error: subprocess.CalledProcessError) -> str | None:
    """Return a bounded tail from captured process output, if one exists."""
    chunks = []
    for value in (error.stdout, error.stderr):
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value not in chunks:
            chunks.append(value)
    lines = [line.strip() for line in "\n".join(chunks).replace("\r", "\n").splitlines() if line.strip()]
    if not lines:
        return None
    tail = " | ".join(lines[-DIAGNOSTIC_MAX_LINES:])
    if len(tail) > DIAGNOSTIC_MAX_CHARACTERS:
        tail = "..." + tail[-(DIAGNOSTIC_MAX_CHARACTERS - 3) :]
    return tail


def _failure_cause(device: str, error: subprocess.CalledProcessError) -> str:
    cause = f"{device} exited with code {error.returncode}"
    diagnostic = _diagnostic_tail(error)
    if diagnostic:
        cause += f"; diagnostic tail: {diagnostic}"
    return cause


def _command(audio_file: Path, staging: Path, device: str) -> list[str]:
    executable = sys.executable
    if getattr(sys, "frozen", False):
        executable = str(frozen_worker_path())
        command = [executable]
    else:
        command = [executable, "-m", "demucs.separate"]
    device_options = []
    if device == "cpu":
        device_options = [
            "--jobs",
            str(_cpu_jobs()),
            "--overlap",
            str(CPU_OVERLAP),
        ]
    return [
        *command,
        "--name",
        MODEL_NAME,
        "--device",
        device,
        *device_options,
        "--out",
        str(staging),
        str(audio_file),
    ]


def _reset_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir()


def _read_stems(staging: Path, audio_file: Path) -> dict[str, bytes]:
    source = staging / MODEL_NAME / audio_file.stem
    missing = [name for name in STEM_NAMES if not (source / name).is_file()]
    if missing:
        raise DemucsSeparationError(
            "demucs.output_incomplete",
            f"Demucs did not produce the required stems: {', '.join(missing)}.",
            "Discard the incomplete result, inspect Demucs diagnostics, and retry the complete song.",
        )
    return {name: (source / name).read_bytes() for name in STEM_NAMES}


def _is_complete_existing_result(output_directory: Path) -> bool:
    """Recognize a previously published four-stem result without mutating it."""
    try:
        if not output_directory.is_dir() or output_directory.is_symlink():
            return False
        entries = tuple(output_directory.iterdir())
        return (
            {entry.name for entry in entries} == set(STEM_NAMES)
            and all(
                entry.is_file() and not entry.is_symlink() and entry.stat().st_size > 0
                for entry in entries
            )
        )
    except OSError:
        return False


def separate_audio(
    audio_file: str | Path,
    output_directory: str | Path,
    *,
    runner: Callable[[Sequence[str]], None] = run_demucs,
    cuda_probe: Callable[[], bool] = cuda_is_available,
) -> Path:
    """Separate one song and atomically create the requested result directory."""
    audio_file = Path(audio_file).resolve()
    output_directory = Path(output_directory).resolve()
    if not audio_file.is_file():
        raise DemucsSeparationError(
            "input.not_file",
            f"The input audio file does not exist: {audio_file}",
            "Select one readable audio file and retry.",
        )
    if output_directory.name in ("", ".", ".."):
        raise DemucsSeparationError(
            "output.invalid",
            "The output directory must have a directory name.",
            "Choose a named output directory and retry.",
        )
    if _is_complete_existing_result(output_directory):
        return output_directory
    output_directory.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stemslayer-demucs-") as temporary:
        staging = Path(temporary) / "demucs"
        staging.mkdir()
        device = "cuda" if cuda_probe() else "cpu"
        try:
            runner(_command(audio_file, staging, device))
        except subprocess.CalledProcessError as cuda_error:
            if device != "cuda":
                raise DemucsSeparationError(
                    "demucs.inference_failed",
                    f"Demucs failed: {_failure_cause('CPU', cuda_error)}.",
                    "Inspect Demucs diagnostics and retry the complete song.",
                ) from cuda_error
            _reset_directory(staging)
            try:
                runner(_command(audio_file, staging, "cpu"))
            except subprocess.CalledProcessError as cpu_error:
                raise DemucsSeparationError(
                    "demucs.inference_failed",
                    "Demucs failed on "
                    f"{_failure_cause('CUDA', cuda_error)}; CPU fallback "
                    f"{_failure_cause('CPU', cpu_error)}.",
                    "Inspect the CUDA and Demucs diagnostics before retrying.",
                ) from cpu_error

        files = _read_stems(staging, audio_file)
        return publish_atomic(output_directory.parent, output_directory.name, files)
