import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from SeparationWorker.engine.pcm import PlanarPCM
from SeparationWorker.engine.publication import publish_atomic
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE, METAL_PROFILE
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.engine.wav import encode_float32_wav
from SeparationWorker.history import HistoryStore, SplitLibraryController, source_sha256
from SeparationWorker.job_manager import JobCancelled, current_job


class Queue:
    def __init__(self):
        self.workers = []
        self.events = []

    def start(self, callback):
        self.workers.append(callback)

    def dispatch(self, callback):
        self.events.append(callback)

    def finish(self):
        self.workers.pop(0)()
        while self.events:
            self.events.pop(0)()


def write_stems(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(STEM_NAMES):
        audio = PlanarPCM(8_000, ((0.1 + index * 0.01, -0.1, 0.05, -0.05),))
        (folder / name).write_bytes(encode_float32_wav(audio))


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = HistoryStore(root / "library.db", root / "library")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, title, *, profile=LEGACY_PROFILE, status="ready", artist="Artist", duration=None):
        record = self.store.create(Path(self.temp.name) / f"{title}.wav", profile)
        return self.store.update(
            record.track_id,
            title=title,
            artist=artist,
            duration_seconds=duration,
            source_hash=title.lower() * 4,
            status=status,
        )

    def test_initializes_versioned_schema_and_nullable_metadata(self):
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            self.assertEqual(2, connection.execute("PRAGMA user_version").fetchone()[0])
        record = self.store.create("untagged.flac", LEGACY_PROFILE)
        self.assertIsNone(record.genre)
        self.assertIsNone(record.duration_seconds)
        self.assertIsNone(record.bpm)
        self.assertIsNone(record.musical_key)
        self.assertEqual(self.store.library_root / record.track_id, record.result_directory)

    def test_queries_search_filter_and_sort_without_interpreting_wildcards(self):
        self.create("Beta", duration=20)
        self.create("Alpha_100%", duration=None)
        self.create("Gamma", status="failed", duration=10)

        self.assertEqual(["Alpha_100%"], [r.title for r in self.store.query(search="_100%")])
        self.assertEqual(["Gamma"], [r.title for r in self.store.query(status="failed")])
        self.assertEqual(
            ["Gamma", "Beta", "Alpha_100%"],
            [r.title for r in self.store.query(sort_by="duration", descending=False)],
        )

    def test_recovers_unfinished_and_preserves_missing_results(self):
        preparing = self.store.create("preparing.wav", LEGACY_PROFILE)
        processing = self.store.create("processing.wav", LEGACY_PROFILE)
        self.store.update(processing.track_id, status="processing")

        self.assertEqual(2, self.store.recover_unfinished())
        self.assertEqual("interrupted", self.store.get(preparing.track_id).status)
        self.assertIn("Retry", self.store.get(processing.track_id).error_detail)

    def test_remove_deletes_only_managed_stems_after_release(self):
        source = Path(self.temp.name) / "source.wav"
        source.write_bytes(b"original")
        record = self.store.create(source, LEGACY_PROFILE)
        record = self.store.update(record.track_id, status="ready")
        record.result_directory.mkdir()
        (record.result_directory / "stem.wav").write_bytes(b"stem")
        released = []

        self.assertTrue(self.store.remove(record.track_id, release=released.append))

        self.assertEqual([record], released)
        self.assertTrue(source.exists())
        self.assertFalse(record.result_directory.exists())
        self.assertIsNone(self.store.get(record.track_id))

    def test_discard_input_copy_is_idempotent_when_no_copy_exists(self):
        appdata = Path(self.temp.name) / "appdata"
        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            self.store.discard_input_copy("missing-track")  # must not raise

    def test_purge_input_copies_removes_orphans_but_keeps_active_jobs(self):
        appdata = Path(self.temp.name) / "appdata"
        active = self.store.create("active.wav", LEGACY_PROFILE)
        self.store.update(active.track_id, status="processing")
        orphan_a = appdata / "inputs" / "orphan-a"
        orphan_b = appdata / "inputs" / "orphan-b"
        active_dir = appdata / "inputs" / active.track_id
        for directory in (orphan_a, orphan_b, active_dir):
            directory.mkdir(parents=True)
            (directory / "source.wav").write_bytes(b"data")

        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            removed = self.store.purge_input_copies()

        self.assertEqual(2, removed)
        self.assertFalse(orphan_a.exists())
        self.assertFalse(orphan_b.exists())
        self.assertTrue(active_dir.exists())

    def test_remove_also_discards_a_lingering_input_copy(self):
        appdata = Path(self.temp.name) / "appdata"
        source = Path(self.temp.name) / "source.wav"
        source.write_bytes(b"original")
        record = self.store.create(source, LEGACY_PROFILE)
        record = self.store.update(record.track_id, status="ready")
        record.result_directory.mkdir()
        (record.result_directory / "stem.wav").write_bytes(b"stem")
        copy_dir = appdata / "inputs" / record.track_id
        copy_dir.mkdir(parents=True)
        (copy_dir / "source.wav").write_bytes(b"stale copy")

        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            self.assertTrue(self.store.remove(record.track_id))

        self.assertFalse(copy_dir.exists())

    def test_remove_refuses_a_catalog_path_outside_the_managed_root(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        protected = outside / "protected.wav"
        protected.write_bytes(b"keep")
        record = self.store.create("source.wav", LEGACY_PROFILE)
        self.store.update(record.track_id, status="ready")
        with closing(sqlite3.connect(self.store.database_path)) as connection, connection:
            connection.execute(
                "UPDATE tracks SET result_directory = ? WHERE track_id = ?",
                (str(outside), record.track_id),
            )

        self.assertFalse(self.store.remove(record.track_id))

        self.assertTrue(protected.exists())
        self.assertEqual("unavailable", self.store.get(record.track_id).status)


class SplitLibraryControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = HistoryStore(root / "library.db", root / "library")
        self.queue = Queue()
        self.successes = []
        self.separations = []

        def separate(source, result, **_options):
            self.separations.append((Path(source), Path(result)))
            write_stems(Path(result))
            return Path(result)

        self.controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=lambda path: {
                "title": Path(path).stem.title(),
                "artist": "Test Artist",
                "genre": None,
                "duration_seconds": None,
            },
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
            on_success=self.successes.append,
        )

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name="song", content=b"audio"):
        path = Path(self.temp.name) / f"{name}.mp3"
        path.write_bytes(content)
        return path

    def add_and_finish(self, source=None):
        record = self.controller.add(source or self.source(), LEGACY_PROFILE.profile_id)
        self.assertEqual("preparing", self.store.get(record.track_id).status)
        self.queue.finish()
        return self.store.get(record.track_id)

    def test_adds_immediately_then_publishes_durable_ready_result(self):
        record = self.add_and_finish()
        self.assertEqual("ready", record.status)
        self.assertEqual("Song", record.title)
        self.assertEqual("Test Artist", record.artist)
        self.assertTrue(record.result_directory.is_dir())
        self.assertEqual([record.track_id], [item.track_id for item in self.successes])

        reopened = HistoryStore(self.store.database_path, self.store.library_root)
        self.assertEqual("ready", reopened.get(record.track_id).status)
        self.assertTrue(reopened.get(record.track_id).result_directory.is_dir())

    def test_content_identity_reuses_ready_but_changed_bytes_create_a_new_result(self):
        source = self.source()
        first = self.add_and_finish(source)
        duplicate = self.controller.add(source, LEGACY_PROFILE.profile_id)
        self.queue.finish()

        self.assertIsNone(self.store.get(duplicate.track_id))
        self.assertEqual(1, len(self.separations))
        self.assertEqual(first.track_id, self.successes[-1].track_id)

        source.write_bytes(b"changed")
        changed = self.add_and_finish(source)
        self.assertNotEqual(first.source_hash, changed.source_hash)
        self.assertEqual(2, len(self.separations))

    def test_same_content_with_different_pipeline_is_not_a_duplicate(self):
        source = self.source()
        first = self.add_and_finish(source)
        metal = self.store.create(source, METAL_PROFILE)
        self.store.update(metal.track_id, source_hash=source_sha256(source), status="failed")

        self.assertNotEqual(first.pipeline_fingerprint, metal.pipeline_fingerprint)
        self.assertIsNone(
            self.store.find_duplicate(first.source_hash, METAL_PROFILE.pipeline_fingerprint, exclude=metal.track_id)
        )

    def test_concurrent_same_identity_adds_run_separation_exactly_once(self):
        source = self.source()
        workers = []
        barrier = threading.Barrier(2)
        successes = []

        def hash_together(path):
            digest = source_sha256(path)
            barrier.wait(timeout=5)
            return digest

        def make_controller(store):
            return SplitLibraryController(
                store,
                separate=self.controller._separate,
                metadata_reader=lambda path: {
                    "title": Path(path).stem,
                    "artist": "Artist",
                    "genre": None,
                    "duration_seconds": None,
                },
                hash_source=hash_together,
                start_worker=workers.append,
                dispatch=lambda callback: callback(),
                on_success=successes.append,
            )

        other_store = HistoryStore(self.store.database_path, self.store.library_root)
        first = make_controller(self.store).add(source, LEGACY_PROFILE.profile_id)
        second = make_controller(other_store).add(source, LEGACY_PROFILE.profile_id)
        threads = [threading.Thread(target=worker) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        records = self.store.query()
        self.assertEqual(1, len(records))
        self.assertEqual("ready", records[0].status)
        self.assertEqual(1, len(self.separations))
        self.assertTrue(successes)
        self.assertEqual({records[0].track_id}, {record.track_id for record in successes})
        self.assertIn(records[0].track_id, {first.track_id, second.track_id})

    def test_remove_is_refused_until_the_active_worker_finishes(self):
        source = self.source()
        record = self.controller.add(source, LEGACY_PROFILE.profile_id)

        self.assertFalse(self.controller.remove(record.track_id))
        self.assertIsNotNone(self.store.get(record.track_id))

        self.queue.finish()
        ready = self.store.get(record.track_id)
        result_directory = ready.result_directory
        self.assertEqual("ready", ready.status)
        self.assertTrue(result_directory.is_dir())

        self.assertTrue(self.controller.remove(record.track_id))
        self.assertFalse(result_directory.exists())
        self.assertIsNone(self.store.get(record.track_id))

    def test_remove_is_refused_while_separation_is_processing(self):
        source = self.source()
        entered = threading.Event()
        release = threading.Event()
        threads = []

        def separate(_source, result, **_options):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            write_stems(Path(result))
            return Path(result)

        def start(target):
            thread = threading.Thread(target=target)
            threads.append(thread)
            thread.start()

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=lambda path: {
                "title": Path(path).stem,
                "artist": "Artist",
                "genre": None,
                "duration_seconds": None,
            },
            start_worker=start,
            dispatch=lambda callback: callback(),
        )
        record = controller.add(source, LEGACY_PROFILE.profile_id)
        self.assertTrue(entered.wait(timeout=5))
        self.assertEqual("processing", self.store.get(record.track_id).status)

        self.assertFalse(controller.remove(record.track_id))
        release.set()
        threads[0].join(timeout=10)

        ready = self.store.get(record.track_id)
        self.assertEqual("ready", ready.status)
        self.assertTrue(ready.result_directory.is_dir())

    def test_failed_duplicate_retries_the_existing_identity(self):
        source = self.source()
        failed = self.store.create(source, LEGACY_PROFILE)
        self.store.update(failed.track_id, source_hash=source_sha256(source), status="failed")

        placeholder = self.controller.add(source, LEGACY_PROFILE.profile_id)
        self.queue.finish()

        self.assertIsNone(self.store.get(placeholder.track_id))
        self.assertEqual("ready", self.store.get(failed.track_id).status)
        self.assertEqual(failed.track_id, self.successes[-1].track_id)

    def test_retry_after_source_bytes_change_transfers_identity_without_stale_reuse(self):
        source = self.source(content=b"original audio")
        original_hash = source_sha256(source)
        attempts = []

        def separate(input_path, result, **_options):
            attempts.append(Path(input_path))
            if len(attempts) == 1:
                raise RuntimeError("first attempt failed")
            write_stems(Path(result))
            return Path(result)

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=lambda path: {
                "title": Path(path).stem,
                "artist": "Artist",
                "genre": None,
                "duration_seconds": None,
            },
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
        )
        record = controller.add(source, LEGACY_PROFILE.profile_id)
        self.queue.finish()
        self.assertEqual("failed", self.store.get(record.track_id).status)

        source.write_bytes(b"changed audio")
        changed_hash = source_sha256(source)
        self.assertTrue(controller.retry(record.track_id))
        self.queue.finish()

        retried = self.store.get(record.track_id)
        self.assertEqual("ready", retried.status)
        self.assertEqual(changed_hash, retried.source_hash)
        self.assertIsNone(
            self.store.find_duplicate(original_hash, LEGACY_PROFILE.pipeline_fingerprint)
        )

        original_copy = self.source("original-copy", content=b"original audio")
        old_identity = controller.add(original_copy, LEGACY_PROFILE.profile_id)
        self.queue.finish()

        old_identity = self.store.get(old_identity.track_id)
        self.assertEqual("ready", old_identity.status)
        self.assertEqual(original_hash, old_identity.source_hash)
        self.assertNotEqual(retried.track_id, old_identity.track_id)
        self.assertEqual(3, len(attempts))

    def test_retry_matching_ready_identity_removes_superseded_owned_assets(self):
        ready_source = self.source("ready", content=b"shared audio")
        ready = self.add_and_finish(ready_source)
        old_source = self.source("failed", content=b"old audio")
        failed = self.store.create(old_source, LEGACY_PROFILE)
        self.store.claim_identity(
            failed.track_id, source_sha256(old_source), LEGACY_PROFILE.pipeline_fingerprint
        )
        failed = self.store.update(failed.track_id, status="unavailable")
        failed.result_directory.mkdir()
        stale_asset = failed.result_directory / "invalid.wav"
        stale_asset.write_bytes(b"invalid")
        old_source.write_bytes(b"shared audio")

        self.assertTrue(self.controller.retry(failed.track_id))
        self.queue.finish()

        self.assertIsNone(self.store.get(failed.track_id))
        self.assertFalse(failed.result_directory.exists())
        self.assertEqual("ready", self.store.get(ready.track_id).status)
        self.assertEqual(1, len(self.separations))
        self.assertEqual(ready.track_id, self.successes[-1].track_id)

    def test_duplicate_candidate_cleanup_failure_preserves_actionable_record(self):
        ready_source = self.source("ready", content=b"shared audio")
        ready = self.add_and_finish(ready_source)
        old_source = self.source("blocked", content=b"old audio")
        blocked = self.store.create(old_source, LEGACY_PROFILE)
        self.store.claim_identity(
            blocked.track_id, source_sha256(old_source), LEGACY_PROFILE.pipeline_fingerprint
        )
        blocked = self.store.update(blocked.track_id, status="failed")
        blocked.result_directory.mkdir()
        (blocked.result_directory / "held-open.wav").write_bytes(b"held")
        old_source.write_bytes(b"shared audio")

        self.assertTrue(self.controller.retry(blocked.track_id))
        with mock.patch(
            "SeparationWorker.history.shutil.rmtree",
            side_effect=PermissionError("file is open"),
        ):
            self.queue.finish()

        preserved = self.store.get(blocked.track_id)
        self.assertEqual("unavailable", preserved.status)
        self.assertIsNone(preserved.source_hash)
        self.assertIn("Close the Mixer", preserved.error_detail)
        self.assertTrue(preserved.result_directory.is_dir())
        self.assertEqual("ready", self.store.get(ready.track_id).status)
        self.assertEqual(1, len(self.separations))

    def test_old_content_add_during_superseded_cleanup_claims_its_own_row(self):
        ready_source = self.source("ready", content=b"new shared audio")
        ready = self.add_and_finish(ready_source)
        old_source = self.source("candidate", content=b"old audio")
        candidate = self.store.create(old_source, LEGACY_PROFILE)
        self.store.claim_identity(
            candidate.track_id, source_sha256(old_source), LEGACY_PROFILE.pipeline_fingerprint
        )
        candidate = self.store.update(candidate.track_id, status="failed")
        candidate.result_directory.mkdir()
        (candidate.result_directory / "invalid.wav").write_bytes(b"invalid")
        old_source.write_bytes(b"new shared audio")

        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()
        retry_threads = []
        original_cleanup = self.store.discard_duplicate_candidate

        def pause_cleanup(track_id):
            cleanup_entered.set()
            if not allow_cleanup.wait(timeout=5):
                raise RuntimeError("timed out waiting to resume candidate cleanup")
            return original_cleanup(track_id)

        def start_retry(target):
            thread = threading.Thread(target=target)
            retry_threads.append(thread)
            thread.start()

        retry_controller = SplitLibraryController(
            self.store,
            separate=self.controller._separate,
            metadata_reader=self.controller._metadata_reader,
            start_worker=start_retry,
            dispatch=lambda callback: callback(),
        )
        with mock.patch.object(
            self.store, "discard_duplicate_candidate", side_effect=pause_cleanup
        ):
            self.assertTrue(retry_controller.retry(candidate.track_id))
            self.assertTrue(cleanup_entered.wait(timeout=5))

            old_copy = self.source("old-copy", content=b"old audio")
            old_controller = SplitLibraryController(
                HistoryStore(self.store.database_path, self.store.library_root),
                separate=self.controller._separate,
                metadata_reader=self.controller._metadata_reader,
                start_worker=lambda callback: callback(),
                dispatch=lambda callback: callback(),
            )
            old_record = old_controller.add(old_copy, LEGACY_PROFILE.profile_id)
            old_record = self.store.get(old_record.track_id)
            self.assertEqual("ready", old_record.status)
            self.assertNotEqual(candidate.track_id, old_record.track_id)

            allow_cleanup.set()
            retry_threads[0].join(timeout=10)

        self.assertFalse(retry_threads[0].is_alive())
        self.assertIsNone(self.store.get(candidate.track_id))
        self.assertEqual("ready", self.store.get(ready.track_id).status)
        self.assertEqual("ready", self.store.get(old_record.track_id).status)
        self.assertEqual(2, len(self.separations))

    def test_missing_ready_result_becomes_actionable_unavailable(self):
        record = self.add_and_finish()
        for child in record.result_directory.iterdir():
            child.unlink()
        record.result_directory.rmdir()

        self.assertIsNone(self.controller.open(record.track_id))
        unavailable = self.store.get(record.track_id)
        self.assertEqual("unavailable", unavailable.status)
        self.assertIn("Retry", unavailable.error_detail)

    def test_mutating_the_source_after_add_cannot_change_the_hash_or_separation_input(self):
        # ARC-02: the file is hashed and then mutated in the same window a
        # real external edit could race the worker. The job's hash and
        # separation input must both bind to the immutable copy, not to
        # whatever `source` contains by the time `separate` runs.
        source = self.source(content=b"original bytes")
        digests_seen = []

        def hash_and_mutate(path):
            digest = source_sha256(path)
            source.write_bytes(b"mutated bytes")
            return digest

        def separate(input_path, result, **_options):
            digests_seen.append(source_sha256(input_path))
            write_stems(Path(result))
            return Path(result)

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
            hash_source=hash_and_mutate,
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
        )
        record = controller.add(source, LEGACY_PROFILE.profile_id)
        self.queue.finish()

        ready = self.store.get(record.track_id)
        self.assertEqual("ready", ready.status)
        self.assertEqual(1, len(digests_seen))
        self.assertEqual(digests_seen[0], ready.source_hash)

    def test_copy_is_created_before_hashing_and_used_for_hashing_and_separation(self):
        source = self.source(content=b"payload")
        appdata = Path(self.temp.name) / "appdata"
        hashed_paths = []
        separated_paths = []
        separated_contents = []

        def hash_source(path):
            hashed_paths.append(Path(path))
            return source_sha256(path)

        def separate(input_path, result, **_options):
            separated_paths.append(Path(input_path))
            separated_contents.append(Path(input_path).read_bytes())
            write_stems(Path(result))
            return Path(result)

        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            controller = SplitLibraryController(
                self.store,
                separate=separate,
                metadata_reader=self.controller._metadata_reader,
                hash_source=hash_source,
                start_worker=self.queue.start,
                dispatch=self.queue.dispatch,
            )
            record = controller.add(source, LEGACY_PROFILE.profile_id)
            expected_copy = appdata / "inputs" / record.track_id / "source.mp3"
            self.queue.finish()

        self.assertEqual([expected_copy], hashed_paths)
        self.assertEqual([expected_copy], separated_paths)
        self.assertEqual([b"payload"], separated_contents)

    def test_input_copy_is_deleted_after_the_job_reaches_ready(self):
        source = self.source()
        appdata = Path(self.temp.name) / "appdata"
        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            record = self.controller.add(source, LEGACY_PROFILE.profile_id)
            copy_dir = appdata / "inputs" / record.track_id
            copy_dir.mkdir(parents=True)
            self.queue.finish()
            ready = self.store.get(record.track_id)
            self.assertEqual("ready", ready.status)
            self.assertFalse(copy_dir.exists())

    def test_input_copy_is_deleted_after_the_job_reaches_failed(self):
        source = self.source()
        appdata = Path(self.temp.name) / "appdata"

        def separate(_input_path, _result, **_options):
            raise RuntimeError("boom")

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
        )
        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            record = controller.add(source, LEGACY_PROFILE.profile_id)
            copy_dir = appdata / "inputs" / record.track_id
            copy_dir.mkdir(parents=True)
            self.queue.finish()
            failed = self.store.get(record.track_id)
            self.assertEqual("failed", failed.status)
            self.assertFalse(copy_dir.exists())

    def test_retry_recopies_the_input_after_deletion_and_succeeds(self):
        source = self.source(content=b"retry me")
        appdata = Path(self.temp.name) / "appdata"
        attempts = []
        staged_contents = []

        def separate(input_path, result, **_options):
            attempts.append(Path(input_path))
            staged_contents.append(Path(input_path).read_bytes())
            if len(attempts) == 1:
                raise RuntimeError("first attempt fails")
            write_stems(Path(result))
            return Path(result)

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
        )
        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            record = controller.add(source, LEGACY_PROFILE.profile_id)
            self.queue.finish()
            self.assertEqual("failed", self.store.get(record.track_id).status)

            self.assertTrue(controller.retry(record.track_id))
            self.queue.finish()

        expected_copy = appdata / "inputs" / record.track_id / "source.mp3"
        retried = self.store.get(record.track_id)
        self.assertEqual("ready", retried.status)
        self.assertEqual([expected_copy, expected_copy], attempts)
        self.assertEqual([b"retry me", b"retry me"], staged_contents)
        self.assertTrue(source.exists())
        self.assertFalse(expected_copy.parent.exists())

    def test_retry_replaces_an_invalid_managed_result_without_touching_the_source(self):
        source = self.source()
        record = self.store.create(source, LEGACY_PROFILE)
        record.result_directory.mkdir()
        (record.result_directory / "conflicting.txt").write_text("broken", encoding="utf-8")
        self.store.update(
            record.track_id,
            source_hash=source_sha256(source),
            status="unavailable",
            error_detail="Stored stems are unavailable",
        )

        self.assertTrue(self.controller.retry(record.track_id))
        self.queue.finish()

        ready = self.store.get(record.track_id)
        self.assertEqual("ready", ready.status)
        self.assertFalse((ready.result_directory / "conflicting.txt").exists())
        self.assertTrue(source.exists())

    def test_cancel_mid_run_lands_on_interrupted_with_no_partial_artifacts_and_retry_still_works(self):
        source = self.source(content=b"cancel me")
        appdata = Path(self.temp.name) / "appdata"
        attempts = []

        def separate(input_path, result, **_options):
            attempts.append(Path(input_path))
            if len(attempts) == 1:
                raise JobCancelled("job cancelled mid separation")
            write_stems(Path(result))
            return Path(result)

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
        )
        with mock.patch("SeparationWorker.history.local_data_root", return_value=appdata):
            record = controller.add(source, LEGACY_PROFILE.profile_id)
            copy_dir = appdata / "inputs" / record.track_id
            copy_dir.mkdir(parents=True)
            self.queue.finish()

            interrupted = self.store.get(record.track_id)
            self.assertEqual("interrupted", interrupted.status)
            self.assertIn("job.cancelled", interrupted.error_detail)
            self.assertFalse(copy_dir.exists())
            self.assertFalse(interrupted.result_directory.exists())

            self.assertTrue(controller.retry(record.track_id))
            self.queue.finish()

        retried = self.store.get(record.track_id)
        self.assertEqual("ready", retried.status)
        self.assertEqual(2, len(attempts))

    def test_commit_if_active_blocks_the_commit_when_cancelled_just_before_publication(self):
        source = self.source(content=b"race the commit")

        def separate(input_path, result, *, cancellation=None, **_options):
            def validate(_staging):
                cancellation.cancel()

            result = Path(result)
            return publish_atomic(
                result.parent,
                result.name,
                {"vocals.wav": b"vocals"},
                cancellation=cancellation,
                validate=validate,
            )

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
            start_worker=self.queue.start,
            dispatch=self.queue.dispatch,
        )
        record = controller.add(source, LEGACY_PROFILE.profile_id)
        self.queue.finish()

        interrupted = self.store.get(record.track_id)
        self.assertEqual("interrupted", interrupted.status)
        self.assertIn("job.cancelled", interrupted.error_detail)
        self.assertFalse(interrupted.result_directory.exists())

    def test_shutdown_cancels_the_running_job_and_drains_three_queued_jobs_within_the_deadline(self):
        entered = threading.Event()
        started_sources = []

        def separate(input_path, result, *, cancellation=None, **_options):
            started_sources.append(Path(input_path))
            entered.set()
            # Unwind only once shutdown() has actually cancelled this job,
            # the way a killed subprocess would. A fixed-delay release raced
            # shutdown()'s queued-job retirement (three SQLite writes) on a
            # slow CI runner and let the job finish "ready" instead.
            deadline = time.monotonic() + 5
            while not (cancellation is not None and cancellation.cancelled):
                if time.monotonic() > deadline:
                    raise RuntimeError("timed out waiting for shutdown() to cancel the running job")
                time.sleep(0.01)
            raise JobCancelled("job cancelled during shutdown drain")

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
            # Real background threads (the default start_worker) are required
            # here: one job must genuinely be running while shutdown() drains
            # the rest concurrently.
        )
        running = controller.add(self.source("running"), LEGACY_PROFILE.profile_id)
        self.assertTrue(entered.wait(timeout=5))
        queued = [
            controller.add(self.source(f"queued-{index}"), LEGACY_PROFILE.profile_id)
            for index in range(3)
        ]

        started_at = time.monotonic()
        joined = controller.shutdown(deadline=3.0)
        elapsed = time.monotonic() - started_at

        self.assertTrue(joined)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(1, len(started_sources))
        interrupted_running = self.store.get(running.track_id)
        self.assertEqual("interrupted", interrupted_running.status)
        for record in queued:
            self.assertEqual("interrupted", self.store.get(record.track_id).status)

    def test_controller_propagates_queued_track_ids_in_fifo_order_as_the_running_job_drains(self):
        # CRITICAL gap (verify-report): SplitLibraryController._queue_changed
        # / LibraryState.queued_track_ids propagation had zero covering test
        # above raw JobManager.on_queue_change. Real background threads (the
        # default start_worker) are required: one job must genuinely run
        # while three more sit behind it in the FIFO queue.
        #
        # Every staged input copy is renamed to "source<ext>" (ARC-02's
        # immutable-copy staging), so jobs are distinguished by call order,
        # not by the input path's name.
        entered = [threading.Event() for _ in range(4)]
        release = [threading.Event() for _ in range(4)]
        started: list[int] = []

        def separate(input_path, result, **_options):
            index = len(started)
            started.append(index)
            entered[index].set()
            self.assertTrue(release[index].wait(timeout=5))
            write_stems(Path(result))
            return Path(result)

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
        )

        # Distinct content per source: identical bytes would hash to the same
        # identity, and once a job actually reaches separation (unlike the
        # shutdown-drain test, which cancels these before they ever run),
        # claim_identity() would collapse later jobs into the first one's
        # record instead of running each independently.
        running = controller.add(self.source("running", content=b"running"), LEGACY_PROFILE.profile_id)
        self.assertTrue(entered[0].wait(timeout=5))

        queued = [
            controller.add(
                self.source(f"queued-{index}", content=f"queued-{index}".encode()),
                LEGACY_PROFILE.profile_id,
            )
            for index in range(3)
        ]
        expected_fifo = tuple(record.track_id for record in queued)

        # Submitting behind a full concurrency slot reports the new queue
        # position immediately (D4) -- without this, the first entry queued
        # behind a running job would show no "Queued" state at all until some
        # other job's start/finish happened to move the queue.
        self.assertEqual(expected_fifo, controller.state.queued_track_ids)

        release[0].set()
        self.assertTrue(entered[1].wait(timeout=5))
        self.assertEqual(expected_fifo[1:], controller.state.queued_track_ids)

        release[1].set()
        self.assertTrue(entered[2].wait(timeout=5))
        self.assertEqual(expected_fifo[2:], controller.state.queued_track_ids)

        release[2].set()
        self.assertTrue(entered[3].wait(timeout=5))
        self.assertEqual((), controller.state.queued_track_ids)

        release[3].set()
        all_records = [running] + queued
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(self.store.get(record.track_id).status == "ready" for record in all_records):
                break
            time.sleep(0.02)
        self.assertTrue(all(self.store.get(record.track_id).status == "ready" for record in all_records))
        self.assertEqual((), controller.state.queued_track_ids)

    def test_cancel_terminates_a_real_running_subprocess_end_to_end_and_lands_on_interrupted(self):
        # WARNING gap (verify-report): SplitLibraryController.cancel(), the
        # exact method wired to the GUI's per-row Cancel button, was never
        # directly invoked by any test end-to-end. This proves the whole
        # path (cancel() -> JobManager.cancel() -> a real owned subprocess
        # actually killed) against a real `python -c "time.sleep(10)"` child,
        # not a mock.
        entered = threading.Event()
        process_holder: dict = {}

        def separate(input_path, result, *, cancellation=None, **_options):
            handle = current_job()
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
            process_holder["process"] = process
            handle.register(process)
            entered.set()
            try:
                process.wait()
            finally:
                handle.unregister(process)
            if cancellation is not None and cancellation.cancelled:
                raise JobCancelled("job cancelled mid separation")
            write_stems(Path(result))
            return Path(result)

        controller = SplitLibraryController(
            self.store,
            separate=separate,
            metadata_reader=self.controller._metadata_reader,
        )
        record = controller.add(self.source("cancel-me"), LEGACY_PROFILE.profile_id)
        self.assertTrue(entered.wait(timeout=5), "the job never started its real child process")

        self.assertTrue(controller.cancel(record.track_id))

        process = process_holder.get("process")
        self.assertIsNotNone(process, "the job never registered its child process")
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll(), "cancel() must actually terminate the real subprocess")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.store.get(record.track_id).status == "interrupted":
                break
            time.sleep(0.02)
        interrupted = self.store.get(record.track_id)
        self.assertEqual("interrupted", interrupted.status)
        self.assertIn("job.cancelled", interrupted.error_detail)


if __name__ == "__main__":
    unittest.main()
