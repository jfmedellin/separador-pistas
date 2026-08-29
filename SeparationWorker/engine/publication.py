import os
import hashlib
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(eq=False)
class PublicationError(RuntimeError):
    code: str
    cause: str
    recovery: str
    retryable: bool = True
    result_name: str | None = None
    expected_result_identity: str | None = None
    manifest_digest: str | None = None
    reconciliation_action: str | None = None

    def __str__(self):
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


@dataclass(frozen=True)
class PublicationExpectation:
    result_identity: str
    manifest_digest: str


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    retryable: bool
    path: Path | None
    expectation: PublicationExpectation


def expected_publication(files):
    inventory = bytearray()
    entries = sorted((name.replace("\\", "/"), data) for name, data in files.items())
    for name, data in entries:
        if not isinstance(data, bytes):
            raise TypeError("Publication files must contain bytes.")
        inventory.extend(name.encode("utf-8"))
        inventory.append(0)
        inventory.extend(hashlib.sha256(data).digest())
    manifest_digest = hashlib.sha256(inventory).hexdigest()
    identity = hashlib.sha256(b"publication-result-v1\0" + bytes.fromhex(manifest_digest)).hexdigest()
    return PublicationExpectation(identity, manifest_digest)


def _reconciliation_error(result_name, expectation, cause):
    return PublicationError(
        "publication.reconciliation_required",
        cause,
        "Inspect the existing result and recover it explicitly; do not retry, overwrite, or delete it.",
        False,
        result_name,
        expectation.result_identity,
        expectation.manifest_digest,
        "Reconcile the existing result with its expected identity and manifest digest.",
    )


def reconcile_publication(destination, result_name, files):
    destination = Path(destination)
    file_parts = {name: _safe_parts(name) for name in files}
    if _safe_parts(result_name, result=True) is None or any(parts is None for parts in file_parts.values()):
        raise PublicationError(
            "publication.path_escape",
            "A result path escapes its destination-local directory.",
            "Use a single relative result name.",
        )
    expectation = expected_publication(files)
    final = destination / result_name
    if not final.exists():
        return ReconciliationResult("absent", True, None, expectation)
    try:
        if not final.is_dir():
            raise OSError("result path is not a directory")
        actual_files = {
            item.relative_to(final).as_posix(): item.read_bytes()
            for item in final.rglob("*")
            if item.is_file()
        }
        expected_names = {"/".join(parts) for parts in file_parts.values()}
        expected_dirs = {
            parent.as_posix()
            for name in expected_names
            for parent in Path(name).parents
            if parent.as_posix() != "."
        }
        actual_dirs = {
            item.relative_to(final).as_posix() for item in final.rglob("*") if item.is_dir()
        }
    except (OSError, ValueError) as exc:
        raise _reconciliation_error(result_name, expectation, f"Existing result is unreadable: {exc}") from exc
    if actual_dirs != expected_dirs or set(actual_files) != expected_names:
        raise _reconciliation_error(result_name, expectation, "Existing result is incomplete or conflicting.")
    expected_files = {"/".join(file_parts[name]): data for name, data in files.items()}
    actual = expected_publication(actual_files)
    expectation = expected_publication(expected_files)
    if actual != expectation:
        raise _reconciliation_error(result_name, expectation, "Existing result identity or manifest digest mismatches.")
    return ReconciliationResult("adopted", False, final, expectation)


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._commit_lock = threading.Lock()

    def cancel(self):
        with self._commit_lock:
            self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()

    def commit_if_active(self, operation):
        """Serialize the final cancellation decision with the atomic rename."""
        with self._commit_lock:
            _cancelled(self)
            return operation()


def _safe_parts(value, *, result=False):
    if not isinstance(value, str) or not value or "\x00" in value or ":" in value:
        return None
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return None
    if result and len(parts) != 1:
        return None
    return parts


def _cancelled(token):
    if token is not None and token.cancelled:
        raise PublicationError(
            "publication.cancelled",
            "Publication was cancelled before the atomic commit.",
            "Restart the job when a complete result is wanted.",
        )


def _fsync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_staging(staging, file_parts, files):
    for name, parts in file_parts.items():
        target = staging.joinpath(*parts)
        if not target.is_file() or target.read_bytes() != files[name]:
            raise PublicationError(
                "publication.validation_failed",
                f"Staged file failed exact validation: {name}",
                "Check storage integrity and retry the complete job.",
            )


def publish_atomic(destination, result_name, files, *, cancellation=None, validate=None):
    destination = Path(destination)
    result_parts = _safe_parts(result_name, result=True)
    file_parts = {name: _safe_parts(name) for name in files}
    if result_parts is None or any(parts is None for parts in file_parts.values()):
        raise PublicationError(
            "publication.path_escape",
            "A result or staged file path escapes its destination-local directory.",
            "Use relative file paths without roots, parent segments, or drive prefixes.",
        )
    if not destination.is_dir():
        raise PublicationError(
            "publication.destination_invalid",
            "The selected publication destination is not an existing directory.",
            "Select an existing writable output folder and retry.",
        )
    final = destination / result_name
    if final.exists():
        return reconcile_publication(destination, result_name, files).path

    expectation = expected_publication(files)

    _cancelled(cancellation)
    staging = Path(tempfile.mkdtemp(prefix=f".{result_name}.staging-", dir=destination))
    try:
        try:
            for name in sorted(files):
                _cancelled(cancellation)
                target = staging.joinpath(*file_parts[name])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(files[name])
                    stream.flush()
                    os.fsync(stream.fileno())
            _validate_staging(staging, file_parts, files)
            if validate is not None:
                validate(staging)
            _cancelled(cancellation)
            _fsync_directory(staging)
        except PublicationError:
            raise
        except Exception as exc:
            raise PublicationError(
                "publication.stage_failed",
                f"Staging or validation failed: {exc}",
                "Check storage and staged output validation, then retry the job.",
            ) from exc

        try:
            commit = lambda: os.replace(staging, final)
            if cancellation is None:
                commit()
            else:
                cancellation.commit_if_active(commit)
        except OSError as exc:
            raise PublicationError(
                "publication.commit_failed",
                f"Atomic directory publication failed: {exc}",
                "Check destination storage, remove no files, and retry the complete job.",
            ) from exc
        try:
            _fsync_directory(destination)
        except OSError as exc:
            raise PublicationError(
                "publication.commit_outcome_uncertain",
                f"The result was renamed but destination durability is uncertain: {exc}",
                "Preserve the result and reconcile it before any further action.",
                False,
                result_name,
                expectation.result_identity,
                expectation.manifest_digest,
                "Reconcile the preserved result; adopt only an exact identity and digest match.",
            ) from exc
        return final
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
