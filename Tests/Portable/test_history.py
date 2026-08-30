import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from SeparationWorker.engine.pcm import PlanarPCM
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE, METAL_PROFILE
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.engine.wav import encode_float32_wav
from SeparationWorker.history import HistoryStore, SplitLibraryController, source_sha256


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


if __name__ == "__main__":
    unittest.main()
