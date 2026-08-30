"""Durable, user-local catalog and lifecycle for separated songs."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from SeparationWorker.demucs_adapter import separate_audio
from SeparationWorker.engine.stem_profile import StemProfile, resolve_profile
from SeparationWorker.engine.stem_session import StemSession


TrackStatus = Literal[
    "preparing", "processing", "ready", "failed", "interrupted", "unavailable"
]
STATUSES: tuple[TrackStatus, ...] = (
    "preparing", "processing", "ready", "failed", "interrupted", "unavailable"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Stemslayer"
    return Path.home() / "AppData" / "Local" / "Stemslayer"


def source_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _tag(tags, *names: str) -> str | None:
    if tags is None:
        return None
    for name in names:
        value = tags.get(name)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def read_metadata(path: str | Path) -> dict[str, str | float | None]:
    """Read lightweight tags without making malformed metadata fatal."""
    source = Path(path)
    title: str | None = None
    artist: str | None = None
    genre: str | None = None
    duration: float | None = None
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(source, easy=True)
        if audio is not None:
            title = _tag(audio.tags, "title")
            artist = _tag(audio.tags, "artist", "albumartist")
            genre = _tag(audio.tags, "genre")
            raw_duration = getattr(getattr(audio, "info", None), "length", None)
            if isinstance(raw_duration, (int, float)) and math.isfinite(raw_duration) and raw_duration > 0:
                duration = float(raw_duration)
    except Exception:
        pass
    return {
        "title": title or source.stem,
        "artist": artist or "Unknown artist",
        "genre": genre,
        "duration_seconds": duration,
    }


@dataclass(frozen=True)
class TrackRecord:
    track_id: str
    source_path: str
    source_hash: str | None
    title: str
    artist: str | None
    genre: str | None
    duration_seconds: float | None
    bpm: float | None
    musical_key: str | None
    created_at_utc: str
    profile_id: str
    pipeline_fingerprint: str
    result_directory: Path
    status: TrackStatus
    error_detail: str | None


_COLUMNS = (
    "track_id, source_path, source_hash, title, artist, genre, duration_seconds, bpm, "
    "musical_key, created_at_utc, profile_id, pipeline_fingerprint, result_directory, "
    "status, error_detail"
)


class HistoryStore:
    """Small transactional SQLite repository; connections never cross threads."""

    SCHEMA_VERSION = 2

    def __init__(self, database_path: str | Path | None = None, library_root: str | Path | None = None):
        root = local_data_root()
        self.database_path = Path(database_path) if database_path is not None else root / "library.db"
        self.library_root = Path(library_root) if library_root is not None else root / "library"
        self._lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.library_root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(f"Library schema {version} is newer than this app supports")
            if version < 1:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tracks (
                        track_id TEXT PRIMARY KEY,
                        source_path TEXT NOT NULL,
                        source_hash TEXT,
                        title TEXT NOT NULL,
                        artist TEXT,
                        genre TEXT,
                        duration_seconds REAL,
                        bpm REAL,
                        musical_key TEXT,
                        created_at_utc TEXT NOT NULL,
                        profile_id TEXT NOT NULL,
                        pipeline_fingerprint TEXT NOT NULL,
                        result_directory TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'preparing','processing','ready','failed','interrupted','unavailable'
                        )),
                        error_detail TEXT
                    );
                    CREATE INDEX IF NOT EXISTS tracks_created_idx ON tracks(created_at_utc DESC);
                    CREATE INDEX IF NOT EXISTS tracks_identity_idx
                        ON tracks(source_hash, pipeline_fingerprint);
                    PRAGMA user_version = 1;
                    """
                )
            if version < 2:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS identity_claims (
                        source_hash TEXT NOT NULL,
                        pipeline_fingerprint TEXT NOT NULL,
                        track_id TEXT NOT NULL UNIQUE REFERENCES tracks(track_id) ON DELETE CASCADE,
                        PRIMARY KEY (source_hash, pipeline_fingerprint)
                    );
                    PRAGMA user_version = 2;
                    """
                )

    @staticmethod
    def _record(row: sqlite3.Row) -> TrackRecord:
        values = dict(row)
        values["result_directory"] = Path(values["result_directory"])
        return TrackRecord(**values)

    def create(self, source_path: str | Path, profile: StemProfile) -> TrackRecord:
        track_id = uuid.uuid4().hex
        record = TrackRecord(
            track_id=track_id,
            source_path=str(Path(source_path)),
            source_hash=None,
            title=Path(source_path).stem,
            artist="Unknown artist",
            genre=None,
            duration_seconds=None,
            bpm=None,
            musical_key=None,
            created_at_utc=utc_now(),
            profile_id=profile.profile_id,
            pipeline_fingerprint=profile.pipeline_fingerprint,
            result_directory=self.library_root / track_id,
            status="preparing",
            error_detail=None,
        )
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                f"INSERT INTO tracks ({_COLUMNS}) VALUES ({','.join('?' for _ in range(15))})",
                tuple(str(value) if isinstance(value, Path) else value for value in record.__dict__.values()),
            )
        return record

    def get(self, track_id: str) -> TrackRecord | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(f"SELECT {_COLUMNS} FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        return self._record(row) if row is not None else None

    def update(self, track_id: str, **changes) -> TrackRecord:
        allowed = set(TrackRecord.__dataclass_fields__) - {"track_id", "result_directory", "created_at_utc"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported track fields: {', '.join(sorted(unknown))}")
        if "status" in changes and changes["status"] not in STATUSES:
            raise ValueError("Unsupported track status")
        if not changes:
            record = self.get(track_id)
            if record is None:
                raise KeyError(track_id)
            return record
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values = list(changes.values()) + [track_id]
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE tracks SET {assignments} WHERE track_id = ?", values)
            if cursor.rowcount != 1:
                raise KeyError(track_id)
        record = self.get(track_id)
        if record is None:
            raise KeyError(track_id)
        return record

    def find_duplicate(self, source_hash: str, pipeline_fingerprint: str, *, exclude: str | None = None) -> TrackRecord | None:
        sql = f"SELECT {_COLUMNS} FROM tracks WHERE source_hash = ? AND pipeline_fingerprint = ?"
        values: list[str] = [source_hash, pipeline_fingerprint]
        if exclude is not None:
            sql += " AND track_id <> ?"
            values.append(exclude)
        sql += " ORDER BY created_at_utc DESC LIMIT 1"
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(sql, values).fetchone()
        return self._record(row) if row is not None else None

    def claim_identity(
        self, track_id: str, source_hash: str, pipeline_fingerprint: str
    ) -> TrackRecord:
        """Atomically assign one catalog row to a content/pipeline identity.

        The claim table is the cross-thread and cross-process serialization
        point. A lazy claim also adopts pre-v2 rows without deleting duplicate
        assets during migration.
        """
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = connection.execute(
                    f"SELECT {_COLUMNS} FROM tracks WHERE track_id = ?", (track_id,)
                ).fetchone()
                if candidate is None:
                    raise KeyError(track_id)
                claim = connection.execute(
                    """
                    SELECT t.* FROM identity_claims c
                    JOIN tracks t ON t.track_id = c.track_id
                    WHERE c.source_hash = ? AND c.pipeline_fingerprint = ?
                    """,
                    (source_hash, pipeline_fingerprint),
                ).fetchone()
                if claim is not None:
                    if claim["track_id"] != track_id:
                        # The requested identity already belongs elsewhere.
                        # Displace this candidate from its prior identity in the
                        # same transaction, before filesystem cleanup begins,
                        # so old-content arrivals cannot attach to a row that is
                        # about to be retired. Keep the row itself for actionable
                        # cleanup failure reporting.
                        connection.execute(
                            "DELETE FROM identity_claims WHERE track_id = ?",
                            (track_id,),
                        )
                        connection.execute(
                            "UPDATE tracks SET source_hash = NULL WHERE track_id = ?",
                            (track_id,),
                        )
                    connection.commit()
                    return self._record(claim)

                existing = connection.execute(
                    f"""
                    SELECT {_COLUMNS} FROM tracks
                    WHERE source_hash = ? AND pipeline_fingerprint = ? AND track_id <> ?
                    ORDER BY CASE status
                        WHEN 'ready' THEN 0
                        WHEN 'processing' THEN 1
                        WHEN 'preparing' THEN 2
                        ELSE 3 END,
                        created_at_utc DESC
                    LIMIT 1
                    """,
                    (source_hash, pipeline_fingerprint, track_id),
                ).fetchone()
                owner = existing if existing is not None else candidate
                owner_id = owner["track_id"]
                if owner_id == track_id:
                    # A failed/interrupted track may be retried after its source
                    # bytes changed. Retire its old identity in this same write
                    # transaction before binding the stable track id to the new
                    # content, so neither identity can observe a partial move.
                    connection.execute(
                        "DELETE FROM identity_claims WHERE track_id = ?",
                        (track_id,),
                    )
                    connection.execute(
                        "UPDATE tracks SET source_hash = ? WHERE track_id = ?",
                        (source_hash, track_id),
                    )
                connection.execute(
                    """
                    INSERT INTO identity_claims (source_hash, pipeline_fingerprint, track_id)
                    VALUES (?, ?, ?)
                    """,
                    (source_hash, pipeline_fingerprint, owner_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        record = self.get(owner_id)
        if record is None:
            raise KeyError(owner_id)
        return record

    def query(
        self,
        *,
        search: str = "",
        profile_id: str | None = None,
        status: str | None = None,
        sort_by: str = "created_at",
        descending: bool | None = None,
    ) -> tuple[TrackRecord, ...]:
        order_columns = {"created_at": "created_at_utc", "title": "title COLLATE NOCASE", "duration": "duration_seconds"}
        if sort_by not in order_columns:
            raise ValueError("Unsupported history sort")
        if descending is None:
            descending = sort_by == "created_at"
        where: list[str] = []
        values: list[str] = []
        if search.strip():
            where.append("(title LIKE ? ESCAPE '\\' COLLATE NOCASE OR artist LIKE ? ESCAPE '\\' COLLATE NOCASE)")
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        if profile_id:
            where.append("profile_id = ?")
            values.append(profile_id)
        if status:
            if status not in STATUSES:
                raise ValueError("Unsupported history status filter")
            where.append("status = ?")
            values.append(status)
        sql = f"SELECT {_COLUMNS} FROM tracks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        nulls = "duration_seconds IS NULL, " if sort_by == "duration" else ""
        sql += f" ORDER BY {nulls}{order_columns[sort_by]} {'DESC' if descending else 'ASC'}, track_id ASC"
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(sql, values).fetchall()
        return tuple(self._record(row) for row in rows)

    def recover_unfinished(self) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE tracks SET status = 'interrupted', error_detail = ? WHERE status IN ('preparing','processing')",
                ("Stemslayer closed before this separation finished. Retry the track.",),
            )
            return cursor.rowcount

    def validate_ready(self) -> int:
        changed = 0
        for record in self.query():
            if record.status != "ready":
                continue
            try:
                profile = resolve_profile(record.profile_id)
                session = StemSession.load(record.result_directory, profile=profile)
                duration = session.duration_seconds
            except Exception as error:
                self.update(
                    record.track_id,
                    status="unavailable",
                    error_detail=f"Stored stems are unavailable: {error}. Retry from the original audio.",
                )
                changed += 1
            else:
                if record.duration_seconds is None and duration > 0:
                    self.update(record.track_id, duration_seconds=duration)
        return changed

    def delete_catalog_entry(self, track_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            return connection.execute("DELETE FROM tracks WHERE track_id = ?", (track_id,)).rowcount == 1

    def discard_duplicate_candidate(self, track_id: str) -> bool:
        """Retire a superseded candidate and its owned assets without orphaning either."""
        record = self.get(track_id)
        if record is None:
            return False
        try:
            if record.result_directory.exists():
                if record.result_directory.resolve().parent != self.library_root.resolve():
                    raise OSError("result path is outside the managed library")
                shutil.rmtree(record.result_directory)
        except OSError as error:
            self.update(
                track_id,
                status="unavailable",
                error_detail=(
                    f"Could not retire previous stored stems: {error}. "
                    "Close the Mixer if this track is open, then retry or remove it."
                ),
            )
            return False
        if self.delete_catalog_entry(track_id):
            return True
        current = self.get(track_id)
        if current is not None:
            self.update(
                track_id,
                status="unavailable",
                error_detail="Stored stems were cleared but the catalog entry could not be retired. Retry removal.",
            )
        return False

    def remove(self, track_id: str, *, release: Callable[[TrackRecord], None] | None = None) -> bool:
        record = self.get(track_id)
        if record is None:
            return False
        if record.status in {"preparing", "processing"}:
            return False
        if release is not None:
            release(record)
        try:
            if record.result_directory.exists():
                if record.result_directory.resolve().parent != self.library_root.resolve():
                    raise OSError("result path is outside the managed library")
                shutil.rmtree(record.result_directory)
        except OSError as error:
            self.update(track_id, status="unavailable", error_detail=f"Could not remove stored stems: {error}")
            return False
        return self.delete_catalog_entry(track_id)


@dataclass(frozen=True)
class LibraryState:
    tracks: tuple[TrackRecord, ...] = ()
    search: str = ""
    profile_id: str | None = None
    status: str | None = None
    sort_by: str = "created_at"
    descending: bool = True


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="stemslayer-library", daemon=True).start()


class SplitLibraryController:
    """Coordinates catalog identity, background separation, and view updates."""

    def __init__(
        self,
        store: HistoryStore,
        *,
        separate: Callable[..., Path] = separate_audio,
        metadata_reader: Callable[[str | Path], dict] = read_metadata,
        hash_source: Callable[[str | Path], str] = source_sha256,
        start_worker: Callable[[Callable[[], None]], None] = _start_thread,
        dispatch: Callable[[Callable[[], None]], None] = lambda callback: callback(),
        on_change: Callable[[LibraryState], None] = lambda _state: None,
        on_success: Callable[[TrackRecord], None] = lambda _record: None,
    ):
        self.store = store
        self._separate = separate
        self._metadata_reader = metadata_reader
        self._hash_source = hash_source
        self._start_worker = start_worker
        self._dispatch = dispatch
        self._on_change = on_change
        self._on_success = on_success
        self.state = LibraryState()
        self.refresh()

    def refresh(self) -> LibraryState:
        self.state = replace(
            self.state,
            tracks=self.store.query(
                search=self.state.search,
                profile_id=self.state.profile_id,
                status=self.state.status,
                sort_by=self.state.sort_by,
                descending=self.state.descending,
            ),
        )
        self._on_change(self.state)
        return self.state

    def set_query(self, *, search=None, profile_id=None, status=None, sort_by=None, descending=None) -> None:
        changes = {}
        if search is not None:
            changes["search"] = str(search)
        if profile_id is not None:
            changes["profile_id"] = None if profile_id in ("", "all") else profile_id
        if status is not None:
            changes["status"] = None if status in ("", "all") else status
        if sort_by is not None:
            changes["sort_by"] = sort_by
            if descending is None:
                changes["descending"] = sort_by == "created_at"
        if descending is not None:
            changes["descending"] = bool(descending)
        self.state = replace(self.state, **changes)
        self.refresh()

    def add(self, source_path: str | Path, profile_id: str) -> TrackRecord:
        profile = resolve_profile(profile_id)
        if not profile.enabled:
            raise ValueError(f"{profile.display_name} is not available")
        record = self.store.create(source_path, profile)
        self.refresh()
        self._start_worker(lambda: self._prepare(record.track_id, profile))
        return record

    def retry(self, track_id: str) -> bool:
        record = self.store.get(track_id)
        if record is None or record.status not in {"failed", "interrupted", "unavailable"}:
            return False
        try:
            profile = resolve_profile(record.profile_id)
        except Exception as error:
            self.store.update(track_id, status="unavailable", error_detail=str(error))
            self.refresh()
            return False
        self.store.update(track_id, status="preparing", error_detail=None)
        self.refresh()
        self._start_worker(lambda: self._prepare(track_id, profile))
        return True

    def _prepare(self, track_id: str, profile: StemProfile) -> None:
        record = self.store.get(track_id)
        if record is None:
            return
        try:
            requested_source = record.source_path
            metadata = self._metadata_reader(requested_source)
            digest = self._hash_source(requested_source)
            owner = self.store.claim_identity(track_id, digest, profile.pipeline_fingerprint)
            if owner.track_id != track_id:
                if not self.store.discard_duplicate_candidate(track_id):
                    self._dispatch(self.refresh)
                    return
                if owner.status == "ready":
                    self._dispatch(lambda: self._reuse_ready(owner.track_id))
                    return
                if owner.status in {"failed", "interrupted", "unavailable"}:
                    track_id = owner.track_id
                    record = owner
                else:
                    self._dispatch(self.refresh)
                    return
            record = self.store.update(
                track_id,
                source_path=requested_source,
                source_hash=digest,
                title=metadata.get("title") or Path(requested_source).stem,
                artist=metadata.get("artist") or "Unknown artist",
                genre=metadata.get("genre"),
                duration_seconds=metadata.get("duration_seconds"),
                status="processing",
                error_detail=None,
            )
            self._dispatch(self.refresh)
            self._discard_invalid_managed_result(record, profile)
            result = Path(self._separate(record.source_path, record.result_directory, profile=profile))
            if result.resolve() != record.result_directory.resolve():
                raise RuntimeError("Separation published outside the managed library directory")
            session = StemSession.load(result, profile=profile)
            duration = record.duration_seconds or session.duration_seconds
            self.store.update(track_id, status="ready", duration_seconds=duration, error_detail=None)
        except Exception as error:
            current = self.store.get(track_id)
            if current is not None:
                cause = getattr(error, "cause", str(error))
                recovery = getattr(error, "recovery", "Retry from the original audio.")
                self.store.update(track_id, status="failed", error_detail=f"{cause} {recovery}".strip())
            self._dispatch(self.refresh)
            return
        self._dispatch(lambda: self._finish_success(track_id))

    def _discard_invalid_managed_result(self, record: TrackRecord, profile: StemProfile) -> None:
        """Clear only an invalid track-owned result so a retry can publish atomically."""
        directory = record.result_directory
        if not directory.exists():
            return
        try:
            StemSession.load(directory, profile=profile)
            return
        except Exception:
            pass
        try:
            managed_root = self.store.library_root.resolve()
            if directory.resolve().parent != managed_root:
                raise RuntimeError("Refusing to clear a result outside the managed library")
            shutil.rmtree(directory)
        except OSError as error:
            raise RuntimeError(f"Could not clear invalid stored stems before retry: {error}") from error

    def _reuse_ready(self, track_id: str) -> None:
        self.refresh()
        record = self.open(track_id)
        if record is not None:
            self._on_success(record)

    def _finish_success(self, track_id: str) -> None:
        self.refresh()
        record = self.store.get(track_id)
        if record is not None:
            self._on_success(record)

    def open(self, track_id: str) -> TrackRecord | None:
        record = self.store.get(track_id)
        if record is None or record.status != "ready":
            return None
        try:
            profile = resolve_profile(record.profile_id)
            StemSession.load(record.result_directory, profile=profile)
        except Exception as error:
            self.store.update(
                track_id,
                status="unavailable",
                error_detail=f"Stored stems are unavailable: {error}. Retry from the original audio.",
            )
            self.refresh()
            return None
        return record

    def remove(self, track_id: str, *, release: Callable[[TrackRecord], None] | None = None) -> bool:
        removed = self.store.remove(track_id, release=release)
        self.refresh()
        return removed
