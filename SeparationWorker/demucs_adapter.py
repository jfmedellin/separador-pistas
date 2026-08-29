"""Small Demucs v4.1 adapter for the Windows stem profiles."""

from __future__ import annotations

import importlib.metadata
import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import soundfile as sf

from SeparationWorker.engine.publication import publish_atomic
from SeparationWorker.engine.role_metrics import (
    DETERMINISTIC_ROLE_THRESHOLDS,
    RoleThresholds,
    is_absent,
    is_audible,
    reconstructs,
)
from SeparationWorker.engine.stereo_split import SPLITTER_ID, split_center_sides
from SeparationWorker.engine.stem_profile import (
    LEGACY_PROFILE,
    MANIFEST_NAME,
    StemProfile,
    StemProfileError,
    build_manifest,
    parse_manifest,
    verify_manifest,
)
from SeparationWorker.engine.stem_session import STEM_NAMES


MODEL_NAME = "htdemucs"
WORKER_EXECUTABLE = "StemslayerWorker.exe"
DIAGNOSTIC_MAX_LINES = 6
DIAGNOSTIC_MAX_CHARACTERS = 800

# Callable receiving the isolated specialist input WAV and a staging directory,
# and writing one WAV per role lane into it.
SpecialistRunner = Callable[[Path, Path], None]

# Deterministic in-process splitters. These need no admission: there are no
# weights, no license, and no download, and their output is a property of the
# arithmetic. Each returns one array per role lane, in the profile's lane order.
SPLITTERS = {SPLITTER_ID: split_center_sides}


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


def _command(
    audio_file: Path,
    staging: Path,
    device: str,
    profile: StemProfile = LEGACY_PROFILE,
) -> list[str]:
    executable = sys.executable
    if getattr(sys, "frozen", False):
        executable = str(frozen_worker_path())
        command = [executable]
    else:
        command = [executable, "-m", "demucs.separate"]
    return [
        *command,
        "--name",
        profile.primary_model,
        "--device",
        device,
        "--out",
        str(staging),
        str(audio_file),
    ]


def _reset_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir()


def _raw_output_paths(staging: Path, audio_file: Path, profile: StemProfile) -> dict[str, Path]:
    """Return one path per raw output the primary model was asked to produce."""
    source = staging / profile.primary_model / audio_file.stem
    names = {name: f"{name}.wav" for name in profile.raw_outputs}
    missing = [file_name for file_name in names.values() if not (source / file_name).is_file()]
    if missing:
        raise DemucsSeparationError(
            "demucs.output_incomplete",
            f"Demucs did not produce the required stems: {', '.join(missing)}.",
            "Discard the incomplete result, inspect Demucs diagnostics, and retry the complete song.",
        )
    return {name: source / file_name for name, file_name in names.items()}


def _read_audio(path: Path) -> tuple[np.ndarray, int, str]:
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        subtype = sf.info(str(path)).subtype
    except (RuntimeError, sf.LibsndfileError, OSError) as exc:
        raise DemucsSeparationError(
            "stem.unreadable",
            f"A separated stem could not be decoded: {path.name} ({exc}).",
            "Discard the incomplete result and retry the complete song.",
        ) from exc
    return data, rate, subtype


def _encode_audio(samples: np.ndarray, rate: int, subtype: str) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, samples, rate, format="WAV", subtype=subtype)
    return buffer.getvalue()


def _fold(paths: Sequence[Path]) -> bytes:
    """Sum several aligned stems into one residual lane."""
    total: np.ndarray | None = None
    rate = 0
    subtype = ""
    for path in paths:
        data, this_rate, this_subtype = _read_audio(path)
        if total is None:
            total, rate, subtype = data.copy(), this_rate, this_subtype
            continue
        if data.shape != total.shape or this_rate != rate:
            raise DemucsSeparationError(
                "stem.misaligned",
                f"Stem {path.name} does not align with the other residual sources.",
                "Discard the result and retry the complete song.",
            )
        total += data
    return _encode_audio(total, rate, subtype)


def _split_roles(profile: StemProfile, source: Path, staging: Path) -> dict[str, Path]:
    """Split one isolated stem into its role lanes with a registered splitter."""
    lanes = tuple(lane for lane in profile.lanes if lane.role_group)
    samples, rate, subtype = _read_audio(source)
    try:
        produced = SPLITTERS[profile.splitter_id](samples)
    except Exception as exc:
        raise DemucsSeparationError(
            "splitter.failed",
            f"The {profile.splitter_id} splitter could not process {source.name}: {exc}.",
            "Discard the result and retry the complete song.",
        ) from exc
    if len(produced) != len(lanes):
        raise DemucsSeparationError(
            "splitter.lane_mismatch",
            f"The {profile.splitter_id} splitter produced {len(produced)} parts for {len(lanes)} lanes.",
            "Register a splitter whose output matches the profile's role lanes.",
        )
    for lane, data in zip(lanes, produced):
        sf.write(str(staging / lane.file_name), data, rate, subtype=subtype)
    return {lane.lane_id: staging / lane.file_name for lane in lanes}


def _role_group(profile: StemProfile) -> str:
    return next(lane.role_group for lane in profile.lanes if lane.role_group)


def _validate_roles(
    profile: StemProfile,
    reference_path: Path,
    role_paths: dict[str, Path],
    thresholds: RoleThresholds,
) -> dict[str, str]:
    """Confirm the role pair reconstructs its source and report absent roles.

    Absence is decided by reconstruction, never by lane energy alone: a silent
    lane whose pair still reproduces the isolated source is a correct result,
    and a silent lane that loses energy is a failure.
    """
    reference, rate, _ = _read_audio(reference_path)
    lanes = profile.role_lanes(_role_group(profile))
    estimate: np.ndarray | None = None
    samples: dict[str, np.ndarray] = {}
    for lane in lanes:
        data, this_rate, _ = _read_audio(role_paths[lane.lane_id])
        if data.shape != reference.shape or this_rate != rate:
            raise DemucsSeparationError(
                "specialist.misaligned",
                f"Role lane {lane.lane_id!r} does not align with its isolated source.",
                "Discard the result and retry; role lanes must match frame zero, rate, channels, and duration.",
            )
        samples[lane.lane_id] = data
        estimate = data.copy() if estimate is None else estimate + data
    if not reconstructs(reference, estimate, thresholds):
        raise DemucsSeparationError(
            "specialist.reconstruction_failed",
            "The role lanes do not reproduce the isolated source within the calibrated limit.",
            "Discard the result; the decomposition lost or duplicated energy.",
        )
    reasons: dict[str, str] = {}
    for lane in lanes:
        data = samples[lane.lane_id]
        if is_absent(data, thresholds):
            reasons[lane.lane_id] = (
                f"No {lane.display_name.lower()} is present in this track; "
                "the role lanes still reconstruct the isolated source."
            )
        elif not is_audible(data, thresholds):
            raise DemucsSeparationError(
                "specialist.lane_not_audible",
                f"Role lane {lane.lane_id!r} carries only a noise floor.",
                "Discard the result; a lane must carry real content or be declared absent.",
            )
    return reasons


def _assemble_lanes(
    profile: StemProfile,
    raw_paths: dict[str, Path],
    role_paths: dict[str, Path],
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for lane in profile.lanes:
        if lane.role_group:
            files[lane.file_name] = role_paths[lane.lane_id].read_bytes()
        elif lane.residual and profile.residual_sources:
            files[lane.file_name] = _fold([raw_paths[name] for name in profile.residual_sources])
        else:
            files[lane.file_name] = raw_paths[lane.lane_id].read_bytes()
    return files


def _is_complete_existing_result(output_directory: Path, file_names: Sequence[str]) -> bool:
    """Recognize a previously published manifest-less result without mutating it."""
    try:
        if not output_directory.is_dir() or output_directory.is_symlink():
            return False
        entries = tuple(output_directory.iterdir())
        return (
            {entry.name for entry in entries} == set(file_names)
            and all(
                entry.is_file() and not entry.is_symlink() and entry.stat().st_size > 0
                for entry in entries
            )
        )
    except OSError:
        return False


def _is_reusable_existing_result(output_directory: Path, profile: StemProfile) -> bool:
    """Recognize a result this exact pipeline may reuse, without mutating it."""
    manifest_file = output_directory / MANIFEST_NAME
    try:
        if manifest_file.is_file():
            manifest = verify_manifest(parse_manifest(manifest_file.read_bytes()), profile)
            published = {entry.name for entry in output_directory.iterdir()}
            return published == set(manifest.file_names) | {MANIFEST_NAME}
    except (StemProfileError, OSError):
        return False
    if profile.accepts_manifestless_results:
        return _is_complete_existing_result(output_directory, profile.file_names)
    return False


def separate_audio(
    audio_file: str | Path,
    output_directory: str | Path,
    *,
    profile: StemProfile = LEGACY_PROFILE,
    runner: Callable[[Sequence[str]], None] = run_demucs,
    cuda_probe: Callable[[], bool] = cuda_is_available,
    specialist: SpecialistRunner | None = None,
    thresholds: RoleThresholds | None = None,
    cancellation=None,
) -> Path:
    """Separate one song and atomically create the requested result directory."""
    if not profile.enabled:
        raise DemucsSeparationError(
            "profile.disabled",
            f"The {profile.display_name} profile is not available in this build.",
            "Choose an enabled profile; a profile stays disabled until its specialist is admitted.",
        )
    if profile.specialist_input is not None:
        if specialist is None:
            raise DemucsSeparationError(
                "profile.specialist_missing",
                f"The {profile.display_name} profile requires a specialist runner.",
                "Admit and pass the registered specialist before separating with this profile.",
            )
        if thresholds is None or not thresholds.calibrated:
            raise DemucsSeparationError(
                "profile.thresholds_uncalibrated",
                f"The {profile.display_name} profile has no calibrated role thresholds.",
                "Calibrate reconstruction, audibility, and absence limits before enabling this profile.",
            )
    if profile.split_input is not None:
        if profile.splitter_id not in SPLITTERS:
            raise DemucsSeparationError(
                "profile.splitter_unregistered",
                f"No splitter is registered under {profile.splitter_id!r}.",
                "Register the splitter this profile names, or choose another profile.",
            )
        # A deterministic split has nothing a corpus could calibrate, so it
        # carries its own limits rather than blocking on trained-model evidence.
        thresholds = thresholds or DETERMINISTIC_ROLE_THRESHOLDS
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
    if _is_reusable_existing_result(output_directory, profile):
        return output_directory
    output_directory.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stemslayer-demucs-") as temporary:
        staging = Path(temporary) / "demucs"
        staging.mkdir()
        device = "cuda" if cuda_probe() else "cpu"
        try:
            runner(_command(audio_file, staging, device, profile))
        except subprocess.CalledProcessError as cuda_error:
            if device != "cuda":
                raise DemucsSeparationError(
                    "demucs.inference_failed",
                    f"Demucs failed: {_failure_cause('CPU', cuda_error)}.",
                    "Inspect Demucs diagnostics and retry the complete song.",
                ) from cuda_error
            _reset_directory(staging)
            try:
                runner(_command(audio_file, staging, "cpu", profile))
            except subprocess.CalledProcessError as cpu_error:
                raise DemucsSeparationError(
                    "demucs.inference_failed",
                    "Demucs failed on "
                    f"{_failure_cause('CUDA', cuda_error)}; CPU fallback "
                    f"{_failure_cause('CPU', cpu_error)}.",
                    "Inspect the CUDA and Demucs diagnostics before retrying.",
                ) from cpu_error

        raw_paths = _raw_output_paths(staging, audio_file, profile)
        role_paths: dict[str, Path] = {}
        absent_lanes: dict[str, str] = {}
        if profile.specialist_input is not None:
            roles = Path(temporary) / "roles"
            roles.mkdir()
            specialist(raw_paths[profile.specialist_input], roles)
            missing = [
                lane.file_name
                for lane in profile.lanes
                if lane.role_group and not (roles / lane.file_name).is_file()
            ]
            if missing:
                raise DemucsSeparationError(
                    "specialist.output_incomplete",
                    f"The specialist did not produce the required role stems: {', '.join(missing)}.",
                    "Discard the incomplete result, inspect specialist diagnostics, and retry.",
                )
            role_paths = {
                lane.lane_id: roles / lane.file_name for lane in profile.lanes if lane.role_group
            }
            absent_lanes = _validate_roles(
                profile, raw_paths[profile.specialist_input], role_paths, thresholds
            )
        elif profile.split_input is not None:
            roles = Path(temporary) / "roles"
            roles.mkdir()
            role_paths = _split_roles(profile, raw_paths[profile.split_input], roles)
            # The split runs through the same gates a trained specialist would
            # face, so there is one validation path rather than a shortcut.
            absent_lanes = _validate_roles(
                profile, raw_paths[profile.split_input], role_paths, thresholds
            )

        files = _assemble_lanes(profile, raw_paths, role_paths)
        if not profile.accepts_manifestless_results:
            manifest = build_manifest(profile, absent_lanes=absent_lanes)
            files[MANIFEST_NAME] = manifest.to_json_bytes()
        return publish_atomic(
            output_directory.parent,
            output_directory.name,
            files,
            cancellation=cancellation,
        )
