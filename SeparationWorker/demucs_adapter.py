"""Small Demucs v4.1 adapter for the Windows four-stem MVP."""

from __future__ import annotations

import importlib.metadata
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
DIAGNOSTIC_MAX_LINES = 6
DIAGNOSTIC_MAX_CHARACTERS = 800


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
    subprocess.run(
        list(command),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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
    return [
        sys.executable,
        "-m",
        "demucs.separate",
        "--name",
        MODEL_NAME,
        "--device",
        device,
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
