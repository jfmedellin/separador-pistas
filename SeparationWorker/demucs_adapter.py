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


MODEL_NAME = "htdemucs"
STEM_NAMES = ("vocals.wav", "drums.wav", "bass.wav", "other.wav")


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
    subprocess.run(list(command), check=True)


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
    output_directory.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="limbus-demucs-") as temporary:
        staging = Path(temporary) / "demucs"
        staging.mkdir()
        device = "cuda" if cuda_probe() else "cpu"
        try:
            runner(_command(audio_file, staging, device))
        except subprocess.CalledProcessError as cuda_error:
            if device != "cuda":
                raise DemucsSeparationError(
                    "demucs.inference_failed",
                    f"Demucs failed on CPU with exit code {cuda_error.returncode}.",
                    "Inspect Demucs diagnostics and retry the complete song.",
                ) from cuda_error
            _reset_directory(staging)
            try:
                runner(_command(audio_file, staging, "cpu"))
            except subprocess.CalledProcessError as cpu_error:
                raise DemucsSeparationError(
                    "demucs.inference_failed",
                    f"Demucs failed on CUDA and CPU fallback exited with code {cpu_error.returncode}.",
                    "Inspect the CUDA and Demucs diagnostics before retrying.",
                ) from cpu_error

        files = _read_stems(staging, audio_file)
        return publish_atomic(output_directory.parent, output_directory.name, files)
