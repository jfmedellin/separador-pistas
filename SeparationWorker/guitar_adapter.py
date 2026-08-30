"""Fail-closed process boundary for the Metal Lead/Rhythm guitar specialist.

`REGISTERED_SPECIALISTS` ships empty on purpose. Admission reviewed the
available public checkpoints and none satisfies the semantic Lead/Rhythm
contract together with redistributable weights and offline Windows
operation. This module therefore defines what an admitted specialist must
satisfy and refuses every job until one is registered with verified local
bytes; it never downloads, never guesses, and never publishes partial work.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from SeparationWorker.demucs_adapter import cuda_is_available
from SeparationWorker.engine.publication import publish_atomic

LEAD_STEM = "lead_guitar.wav"
RHYTHM_STEM = "rhythm_guitar.wav"
ROLE_STEM_NAMES = (LEAD_STEM, RHYTHM_STEM)
DIAGNOSTIC_MAX_LINES = 6
DIAGNOSTIC_MAX_CHARACTERS = 800


@dataclass(eq=False)
class GuitarSpecialistError(RuntimeError):
    code: str
    cause: str
    recovery: str

    def __str__(self) -> str:
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


@dataclass(frozen=True)
class SpecialistEntry:
    """Immutable registration of one admitted Lead/Rhythm specialist."""

    specialist_id: str
    entrypoint: str
    model_file: str
    sha256: str
    network_required: bool = False


@dataclass(frozen=True)
class ResolvedSpecialist:
    specialist_id: str
    entrypoint: str
    model_path: Path
    sha256: str


# Empty until a specialist passes Compliance/evidence/metal-guitar/CONTRACT.md.
REGISTERED_SPECIALISTS: Mapping[str, SpecialistEntry] = {}


def _error(code: str, cause: str, recovery: str) -> GuitarSpecialistError:
    return GuitarSpecialistError(code, cause, recovery)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _error(
            "specialist.asset_unreadable",
            f"The registered specialist bytes could not be read: {exc}",
            "Restore the approved specialist assets and retry.",
        ) from exc
    return digest.hexdigest()


def resolve_specialist(
    specialist_id: str,
    asset_root: str | Path,
    *,
    registry: Mapping[str, SpecialistEntry] | None = None,
) -> ResolvedSpecialist:
    """Return an admitted specialist, or fail closed with actionable remediation."""
    registry = REGISTERED_SPECIALISTS if registry is None else registry
    if not isinstance(specialist_id, str) or not specialist_id:
        raise _error(
            "specialist.invalid_id",
            "A specialist identifier is required.",
            "Request a registered specialist identifier and retry.",
        )
    entry = registry.get(specialist_id)
    if entry is None:
        raise _error(
            "specialist.unregistered",
            f"No Lead/Rhythm specialist is registered under {specialist_id!r}.",
            "Admit a specialist with Tools/admit_metal_guitar_model.py before enabling Metal.",
        )
    if entry.network_required:
        raise _error(
            "specialist.network_required",
            f"Specialist {specialist_id!r} declares a network requirement.",
            "Bundle every required byte locally; inference must make no network request.",
        )
    root = Path(asset_root).resolve()
    candidate = root / entry.model_file
    if candidate.is_symlink():
        raise _error(
            "specialist.path_escape",
            "The registered specialist path is a symbolic link.",
            "Register resolved bytes inside the asset root and retry.",
        )
    path = candidate.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise _error(
            "specialist.path_escape",
            "The registered specialist path escapes the bundled asset root.",
            "Register resolved bytes inside the asset root and retry.",
        ) from None
    if not path.is_file():
        raise _error(
            "specialist.asset_missing",
            f"Registered specialist bytes are missing at {entry.model_file!r}.",
            "Restore the approved specialist assets and retry.",
        )
    digest = _sha256_file(path)
    if digest != entry.sha256:
        raise _error(
            "specialist.hash_mismatch",
            "Specialist bytes do not match their registered SHA-256.",
            "Restore the approved specialist assets and re-run admission.",
        )
    return ResolvedSpecialist(specialist_id, entry.entrypoint, path, digest)


def _diagnostic_tail(error: subprocess.CalledProcessError) -> str | None:
    """Return a bounded tail from captured process output, if one exists."""
    chunks: list[str] = []
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


def run_specialist(command: Sequence[str]) -> None:
    """Run one registered specialist entrypoint without a shell or downloads."""
    options = {
        "check": True,
        "shell": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            options["creationflags"] = creationflags
    subprocess.run(list(command), **options)


def _command(
    specialist: ResolvedSpecialist,
    guitar_file: Path,
    staging: Path,
    device: str,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        specialist.entrypoint,
        "--model",
        str(specialist.model_path),
        "--device",
        device,
        "--out",
        str(staging),
        str(guitar_file),
    ]


def _read_role_stems(staging: Path) -> dict[str, bytes]:
    missing = [name for name in ROLE_STEM_NAMES if not (staging / name).is_file()]
    if missing:
        raise _error(
            "specialist.output_incomplete",
            f"The specialist did not produce the required role stems: {', '.join(missing)}.",
            "Discard the incomplete result, inspect specialist diagnostics, and retry the complete song.",
        )
    return {name: (staging / name).read_bytes() for name in ROLE_STEM_NAMES}


def _cancelled(cancellation) -> None:
    if cancellation is not None and cancellation.cancelled:
        raise _error(
            "specialist.cancelled",
            "The Lead/Rhythm job was cancelled before publication.",
            "Restart the job when a complete result is wanted.",
        )


def separate_guitar_roles(
    guitar_file: str | Path,
    output_directory: str | Path,
    *,
    specialist_id: str,
    asset_root: str | Path,
    runner: Callable[[Sequence[str]], None] = run_specialist,
    cuda_probe: Callable[[], bool] = cuda_is_available,
    cancellation=None,
    registry: Mapping[str, SpecialistEntry] | None = None,
) -> Path:
    """Decompose one isolated guitar-family WAV into Lead and Rhythm role stems.

    The specialist gate runs before the input is read, so an unadmitted
    Metal profile never touches user audio and never creates output.
    """
    specialist = resolve_specialist(specialist_id, asset_root, registry=registry)
    guitar_file = Path(guitar_file).resolve()
    output_directory = Path(output_directory).resolve()
    if not guitar_file.is_file():
        raise _error(
            "input.not_file",
            f"The isolated guitar input does not exist: {guitar_file}",
            "Run primary separation first and retry with its guitar output.",
        )
    if output_directory.name in ("", ".", ".."):
        raise _error(
            "output.invalid",
            "The output directory must have a directory name.",
            "Choose a named output directory and retry.",
        )
    output_directory.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stemslayer-guitar-") as temporary:
        staging = Path(temporary) / "roles"
        staging.mkdir()
        device = "cuda" if cuda_probe() else "cpu"
        _cancelled(cancellation)
        try:
            runner(_command(specialist, guitar_file, staging, device))
        except subprocess.CalledProcessError as failure:
            cause = f"{device.upper()} exited with code {failure.returncode}"
            diagnostic = _diagnostic_tail(failure)
            if diagnostic:
                cause += f"; diagnostic tail: {diagnostic}"
            raise _error(
                "specialist.inference_failed",
                f"The Lead/Rhythm specialist failed: {cause}.",
                "Inspect specialist diagnostics and retry the complete song.",
            ) from failure

        files = _read_role_stems(staging)
        _cancelled(cancellation)
        return publish_atomic(
            output_directory.parent,
            output_directory.name,
            files,
            cancellation=cancellation,
        )
