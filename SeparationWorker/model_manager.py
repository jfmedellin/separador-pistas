"""App-owned acquisition and verification of Demucs model weights (SEC-01).

`ensure_model()` is the only path that may place bytes under the app-owned
cache directory `demucs_adapter._command()` later passes to Demucs via
`--repo <verified-dir>`. This structurally prevents `RemoteRepo`/
`torch.hub.load_state_dict_from_url` from ever running: Demucs never
consults the network or `torch.hub` cache when a `--repo` is given.

Every file is re-verified against `model_manifest.REGISTERED_MODELS`' pinned
SHA-256 on *every* call (D6), not only right after a download, so a later
compromise of the on-disk cache is caught before the tampered bytes ever
reach `--repo`. Acquisition fails closed: any hash mismatch, unregistered
model name, non-HTTPS URL, or interrupted download raises
`ModelAcquisitionError` and leaves no partial `models/{name}` directory
behind (staging happens in a throwaway `mkdtemp()` directory that is only
ever placed with `os.replace()` once every file has verified, D8).

This module deliberately defines its own error type rather than importing
`DemucsSeparationError` from `demucs_adapter`, because `demucs_adapter`
imports this module -- `guitar_adapter.GuitarSpecialistError` sets the same
precedent (D3).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Mapping

from SeparationWorker.model_manifest import REGISTERED_MODELS, ModelEntry, ModelFile
from SeparationWorker.paths import local_data_root

CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30
# Generous relative to the largest registered Demucs weight (~80 MB); this
# only guards against a compromised mirror streaming an unbounded response.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(eq=False)
class ModelAcquisitionError(RuntimeError):
    code: str
    cause: str
    recovery: str

    def __str__(self) -> str:
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


def _error(code: str, cause: str, recovery: str) -> ModelAcquisitionError:
    return ModelAcquisitionError(code, cause, recovery)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_cache_root() -> Path:
    return local_data_root() / "models"


def _demucs_remote_dir() -> Path:
    """Return the vendored `demucs/remote/` directory bundled with Demucs itself."""
    import demucs

    return Path(demucs.__file__).resolve().parent / "remote"


def _open_https_url(url: str) -> IO[bytes]:
    import urllib.request

    return urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)  # noqa: S310 (https enforced by caller)


def _verify_existing(repo_dir: Path, entry: ModelEntry) -> bool:
    """Return True only if every registered file is present with a matching hash."""
    for file in entry.files:
        path = repo_dir / file.file_name
        if not path.is_file():
            return False
        try:
            if _sha256_file(path) != file.sha256:
                return False
        except OSError:
            return False
    return True


def _verify_or_raise(destination: Path, file: ModelFile) -> None:
    try:
        digest = _sha256_file(destination)
    except OSError as exc:
        raise _error(
            "model.hash_mismatch",
            f"{file.file_name!r} could not be read back for verification: {exc}",
            "Retry acquisition; a compromised or truncated cache must never be trusted.",
        ) from exc
    if digest != file.sha256:
        raise _error(
            "model.hash_mismatch",
            f"{file.file_name!r} does not match its registered SHA-256.",
            "Retry acquisition; a compromised mirror or on-disk cache must never be trusted.",
        )


def _copy_bundled_file(file: ModelFile, destination: Path, bundled_root: Path) -> None:
    source = bundled_root / file.file_name
    try:
        shutil.copyfile(source, destination)
    except OSError as exc:
        raise _error(
            "model.bundled_asset_missing",
            f"The bundled asset {file.file_name!r} could not be read from {source}: {exc}",
            "Reinstall the complete Stemslayer portable bundle and retry.",
        ) from exc


def _fetch_downloaded_file(
    opener: Callable[[str], IO[bytes]],
    file: ModelFile,
    destination: Path,
    on_progress: Callable[[str, int, int], None] | None,
) -> None:
    if not file.urls:
        raise _error(
            "model.no_download_url",
            f"No download URL is registered for {file.file_name!r}.",
            "Register at least one https:// URL for this file in the model manifest.",
        )
    for url in file.urls:
        if not url.lower().startswith("https://"):
            raise _error(
                "model.insecure_url",
                f"Refusing a non-HTTPS download URL for {file.file_name!r}: {url}",
                "Register only https:// URLs in the model manifest.",
            )

    url = file.urls[0]
    try:
        source = opener(url)
    except OSError as exc:
        raise _error(
            "model.download_failed",
            f"Could not download {file.file_name!r}: {exc}",
            "Check network connectivity and retry.",
        ) from exc

    total_expected = 0
    headers = getattr(source, "headers", None)
    if headers is not None:
        try:
            total_expected = int(headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            total_expected = 0

    written = 0
    try:
        with destination.open("wb") as sink:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise _error(
                        "model.download_too_large",
                        f"{file.file_name!r} exceeded the maximum allowed download size.",
                        "Verify the manifest URL points at the correct, correctly-sized file.",
                    )
                sink.write(chunk)
                if on_progress is not None:
                    on_progress(file.file_name, written, total_expected)
    except ModelAcquisitionError:
        raise
    except OSError as exc:
        raise _error(
            "model.download_failed",
            f"The download of {file.file_name!r} failed or was interrupted: {exc}",
            "Check network connectivity and retry.",
        ) from exc
    finally:
        try:
            source.close()
        except Exception:
            pass


def ensure_model(
    model_name: str,
    *,
    cache_root: Path | None = None,
    registry: Mapping[str, ModelEntry] | None = None,
    opener: Callable[[str], IO[bytes]] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    bundled_root: Path | None = None,
) -> Path:
    """Return a verified `LocalRepo`-shaped directory for `model_name`, fail closed.

    `bundled_root` is an apply-time addition to the design's four-parameter
    signature: it lets tests point D7's "bundled" `.yaml` copy step at a
    throwaway directory instead of the real vendored `demucs/remote/`, the
    same way `cache_root`/`registry`/`opener` keep the network and
    `%LOCALAPPDATA%` out of unit tests. It defaults to the real vendored
    directory, so every production call site is unaffected.
    """
    registry = REGISTERED_MODELS if registry is None else registry
    entry = registry.get(model_name)
    if entry is None:
        raise _error(
            "model.unregistered",
            f"No model is registered under {model_name!r}.",
            "Register the model's manifest entry before requesting separation with this profile.",
        )

    cache_root = cache_root if cache_root is not None else _default_cache_root()
    repo_dir = cache_root / model_name

    if repo_dir.is_dir() and _verify_existing(repo_dir, entry):
        return repo_dir

    if repo_dir.exists():
        shutil.rmtree(repo_dir)

    opener = opener if opener is not None else _open_https_url
    bundled_root = bundled_root if bundled_root is not None else _demucs_remote_dir()

    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"stemslayer-model-{model_name}-", dir=str(cache_root)))
    try:
        for file in entry.files:
            destination = staging / file.file_name
            if file.source == "bundled":
                _copy_bundled_file(file, destination, bundled_root)
            elif file.source == "download":
                _fetch_downloaded_file(opener, file, destination, on_progress)
            else:
                raise _error(
                    "model.invalid_source",
                    f"Unknown source {file.source!r} registered for {file.file_name!r}.",
                    "Fix the model manifest entry for this file's source.",
                )
            _verify_or_raise(destination, file)
        os.replace(staging, repo_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return repo_dir
