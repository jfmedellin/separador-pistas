"""Temporary per-song stem cache directories with orphan sweep and safe discard.

Every separated song is written to a deterministic, content-addressed
directory under a single shared cache root so a repeated separation of the
same input file can reuse the previous result without re-running Demucs.
Discard and sweep are both scoped strictly to that root so a caller can never
accidentally delete something outside the cache.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from .stem_profile import LEGACY_PROFILE, LEGACY_PROFILE_ID, StemProfile


ORPHAN_MAX_AGE_SECONDS = 6 * 60 * 60


def cache_root() -> Path:
    """Return the shared root directory for temporary stem cache results."""
    return Path(tempfile.gettempdir()) / "limbus-stem-cache"


def _is_original_legacy_pipeline(profile: StemProfile) -> bool:
    """Report whether a profile is the exact pipeline the legacy namespace holds."""
    return (
        profile.profile_id == LEGACY_PROFILE_ID
        and profile.pipeline_fingerprint == LEGACY_PROFILE.pipeline_fingerprint
    )


def cache_key(audio_file: str | Path, profile: StemProfile | None = None) -> str:
    """Return a stable, filesystem-safe identifier for one input and pipeline.

    The key is a hex SHA-256 digest, so it is always free of path separators,
    drive markers, extension dots, and NUL bytes. It is deterministic for
    equivalent paths (relative vs. resolved, case variants on a
    case-insensitive filesystem) and differs for different inputs.

    The unmodified legacy pipeline keeps the input-only key it has always
    used, so cache folders published before profiles existed still match.
    Every other pipeline mixes its fingerprint into the key, so results are
    never reused across profiles, models, or specialist versions.
    """
    resolved = str(Path(audio_file).resolve())
    normalized = os.path.normcase(resolved)
    input_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if profile is None or _is_original_legacy_pipeline(profile):
        return input_digest
    namespaced = f"{input_digest}\0{profile.pipeline_fingerprint}"
    return hashlib.sha256(namespaced.encode("utf-8")).hexdigest()


def cache_directory(audio_file: str | Path, profile: StemProfile | None = None) -> Path:
    """Return the deterministic cache directory for one input and pipeline."""
    return cache_root() / cache_key(audio_file, profile)


def prepare_cache_directory(audio_file: str | Path, profile: StemProfile | None = None) -> Path:
    """Ensure the shared cache root exists and return this file's directory.

    Only the shared root is created here; the leaf result directory itself is
    published atomically by ``demucs_adapter.separate_audio``.
    """
    cache_root().mkdir(parents=True, exist_ok=True)
    return cache_directory(audio_file, profile)


def discard(directory: str | Path) -> bool:
    """Remove one cache directory, refusing anything outside the cache root.

    Returns True only when the directory was actually removed. A path that is
    not a direct child of ``cache_root()`` (including the root itself) is
    refused. A missing or locked directory returns False without raising.
    """
    try:
        candidate = Path(directory)
        root = cache_root().resolve()
        resolved = candidate.resolve()
    except OSError:
        return False
    if resolved.parent != root:
        return False
    try:
        if not resolved.is_dir():
            return False
    except OSError:
        return False
    try:
        shutil.rmtree(resolved)
    except OSError:
        return False
    return not resolved.exists()


def sweep_orphans(
    *,
    root: str | Path | None = None,
    now: float | None = None,
    max_age: float = ORPHAN_MAX_AGE_SECONDS,
) -> list[Path]:
    """Delete stale entries directly under the cache root.

    Only entries whose modification time is older than ``max_age`` seconds
    are removed; the root itself is never a sweep target since only its
    direct children are ever visited. A missing root is tolerated and
    returns an empty list. Returns the paths that were actually removed.
    """
    root = Path(root) if root is not None else cache_root()
    if now is None:
        now = time.time()
    removed: list[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age <= max_age:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            continue
        removed.append(entry)
    return removed
