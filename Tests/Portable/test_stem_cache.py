import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from SeparationWorker.engine.stem_cache import (
    ORPHAN_MAX_AGE_SECONDS,
    cache_directory,
    cache_key,
    cache_root,
    discard,
    prepare_cache_directory,
    sweep_orphans,
)


class CacheKeyTests(unittest.TestCase):
    def test_key_is_free_of_path_separators_and_reserved_characters(self):
        key = cache_key(r"C:\songs\My Song.mp3")

        for character in "/\\:.\x00":
            self.assertNotIn(character, key)

    def test_key_is_deterministic_for_the_same_input(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "song.mp3"

            self.assertEqual(cache_key(audio), cache_key(audio))
            self.assertEqual(cache_key(str(audio)), cache_key(audio))

    def test_key_is_stable_across_normcase_variants(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "Song.mp3"
            lower = str(audio).lower()
            upper = str(audio).upper()

            self.assertEqual(cache_key(lower), cache_key(upper))

    def test_key_differs_for_different_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "first.mp3"
            second = Path(root) / "second.mp3"

            self.assertNotEqual(cache_key(first), cache_key(second))


class CacheDirectoryTests(unittest.TestCase):
    def test_cache_directory_is_the_key_under_the_cache_root(self):
        audio = Path("song.mp3")

        self.assertEqual(cache_root() / cache_key(audio), cache_directory(audio))

    def test_prepare_cache_directory_creates_only_the_shared_root(self):
        with tempfile.TemporaryDirectory() as fake_root:
            fake_root = Path(fake_root) / "cache"
            with mock.patch("SeparationWorker.engine.stem_cache.cache_root", return_value=fake_root):
                audio = Path("song.mp3")

                result = prepare_cache_directory(audio)

                self.assertEqual(fake_root / cache_key(audio), result)
                self.assertTrue(fake_root.is_dir())
                self.assertFalse(result.exists())


class DiscardTests(unittest.TestCase):
    def test_refuses_a_path_that_is_not_a_direct_child_of_cache_root(self):
        with tempfile.TemporaryDirectory() as fake_root:
            fake_root = Path(fake_root) / "cache"
            fake_root.mkdir()
            grandchild = fake_root / "child" / "grandchild"
            grandchild.mkdir(parents=True)
            outside = Path(tempfile.mkdtemp())
            try:
                with mock.patch("SeparationWorker.engine.stem_cache.cache_root", return_value=fake_root):
                    self.assertFalse(discard(grandchild))
                    self.assertTrue(grandchild.exists())
                    self.assertFalse(discard(outside))
                    self.assertTrue(outside.exists())
                    self.assertFalse(discard(fake_root))
                    self.assertTrue(fake_root.exists())
            finally:
                import shutil

                shutil.rmtree(outside, ignore_errors=True)

    def test_removes_a_direct_child_of_cache_root(self):
        with tempfile.TemporaryDirectory() as fake_root:
            fake_root = Path(fake_root) / "cache"
            fake_root.mkdir()
            child = fake_root / "abc123"
            child.mkdir()
            with mock.patch("SeparationWorker.engine.stem_cache.cache_root", return_value=fake_root):
                self.assertTrue(discard(child))
            self.assertFalse(child.exists())

    def test_discard_on_missing_directory_returns_false_without_raising(self):
        with tempfile.TemporaryDirectory() as fake_root:
            fake_root = Path(fake_root) / "cache"
            fake_root.mkdir()
            missing = fake_root / "does-not-exist"
            with mock.patch("SeparationWorker.engine.stem_cache.cache_root", return_value=fake_root):
                self.assertFalse(discard(missing))

    def test_discard_on_locked_directory_returns_false_without_raising(self):
        with tempfile.TemporaryDirectory() as fake_root:
            fake_root = Path(fake_root) / "cache"
            fake_root.mkdir()
            child = fake_root / "abc123"
            child.mkdir()
            with mock.patch("SeparationWorker.engine.stem_cache.cache_root", return_value=fake_root):
                with mock.patch(
                    "SeparationWorker.engine.stem_cache.shutil.rmtree",
                    side_effect=OSError("sharing violation"),
                ):
                    self.assertFalse(discard(child))
            self.assertTrue(child.exists())


class SweepOrphansTests(unittest.TestCase):
    def test_sweeps_only_entries_older_than_max_age(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            stale = root / "stale-run"
            fresh = root / "fresh-run"
            stale.mkdir()
            fresh.mkdir()
            now = time.time()
            old_time = now - ORPHAN_MAX_AGE_SECONDS - 10
            os.utime(stale, (old_time, old_time))

            removed = sweep_orphans(root=root, now=now)

            self.assertEqual([stale], removed)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())

    def test_preserves_fresh_staging_directories(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            staging = root / ".deadbeef.staging-abc123"
            staging.mkdir()

            removed = sweep_orphans(root=root, now=time.time())

            self.assertEqual([], removed)
            self.assertTrue(staging.exists())

    def test_removes_stale_staging_directories(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            staging = root / ".deadbeef.staging-abc123"
            staging.mkdir()
            now = time.time()
            old_time = now - ORPHAN_MAX_AGE_SECONDS - 10
            os.utime(staging, (old_time, old_time))

            removed = sweep_orphans(root=root, now=now)

            self.assertEqual([staging], removed)
            self.assertFalse(staging.exists())

    def test_tolerates_a_missing_root(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "does-not-exist"

            self.assertEqual([], sweep_orphans(root=missing))

    def test_never_targets_the_root_itself(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            now = time.time()
            very_old = now - ORPHAN_MAX_AGE_SECONDS * 10
            os.utime(root, (very_old, very_old))

            removed = sweep_orphans(root=root, now=now)

            self.assertEqual([], removed)
            self.assertTrue(root.exists())


if __name__ == "__main__":
    unittest.main()
