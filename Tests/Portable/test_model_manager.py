"""Regression tests for SEC-01 app-owned model acquisition and verification.

`ensure_model()` MUST fail closed on any hash mismatch, unregistered model,
or non-HTTPS URL, MUST leave no partial `models/{name}` directory behind a
failed acquisition, and MUST NOT touch the network again once a model is
already verified on disk.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from SeparationWorker.model_manager import ModelAcquisitionError, ensure_model
from SeparationWorker.model_manifest import ModelEntry, ModelFile

WEIGHT_BYTES = b"pretend-weight-bytes"
WEIGHT_SHA256 = hashlib.sha256(WEIGHT_BYTES).hexdigest()


def make_registry(*, url="https://example.invalid/weights.th", sha256=WEIGHT_SHA256):
    entry = ModelEntry(
        model_name="fake-model",
        files=(
            ModelFile(
                file_name="fake-model.th",
                sha256=sha256,
                source="download",
                urls=(url,),
            ),
        ),
    )
    return {"fake-model": entry}


class FakeOpener:
    """Records every URL it was asked to open; returns configured bytes."""

    def __init__(self, payload=WEIGHT_BYTES, *, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("simulated network failure")
        return io.BytesIO(self.payload)


class ModelManagerDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp.name) / "models"

    def tearDown(self):
        self.temp.cleanup()

    def ensure(self, **overrides):
        settings = {
            "cache_root": self.cache_root,
            "registry": make_registry(),
            "opener": FakeOpener(),
        }
        settings.update(overrides)
        return ensure_model("fake-model", **settings)

    def test_hash_mismatch_fails_closed_with_no_partial_repo_dir(self):
        with self.assertRaises(ModelAcquisitionError) as caught:
            self.ensure(registry=make_registry(sha256="0" * 64))

        self.assertEqual("model.hash_mismatch", caught.exception.code)
        self.assertFalse((self.cache_root / "fake-model").exists())

    def test_unregistered_model_name_is_rejected(self):
        with self.assertRaises(ModelAcquisitionError) as caught:
            ensure_model(
                "does-not-exist",
                cache_root=self.cache_root,
                registry=make_registry(),
                opener=FakeOpener(),
            )

        self.assertEqual("model.unregistered", caught.exception.code)

    def test_non_https_url_is_rejected_before_any_fetch_attempt(self):
        opener = FakeOpener()

        with self.assertRaises(ModelAcquisitionError) as caught:
            self.ensure(registry=make_registry(url="http://example.invalid/weights.th"), opener=opener)

        self.assertEqual("model.insecure_url", caught.exception.code)
        self.assertEqual([], opener.calls)
        self.assertFalse((self.cache_root / "fake-model").exists())

    def test_mid_download_failure_leaves_no_partial_repo_directory(self):
        with self.assertRaises(ModelAcquisitionError) as caught:
            self.ensure(opener=FakeOpener(fail=True))

        self.assertEqual("model.download_failed", caught.exception.code)
        self.assertFalse((self.cache_root / "fake-model").exists())
        # No leftover staging directory should survive under the cache root either.
        if self.cache_root.exists():
            self.assertEqual([], list(self.cache_root.iterdir()))

    def test_verified_cache_is_reused_without_calling_the_opener_again(self):
        first_opener = FakeOpener()
        first = self.ensure(opener=first_opener)
        self.assertEqual(1, len(first_opener.calls))
        self.assertTrue((first / "fake-model.th").is_file())

        def fail_if_called(_url):
            raise AssertionError("The opener must not be called for an already-verified model")

        second = self.ensure(opener=fail_if_called)

        self.assertEqual(first, second)

    def test_second_profile_acquires_independently_of_the_first(self):
        other_bytes = b"other-weights"
        other_entry = ModelEntry(
            model_name="other-model",
            files=(
                ModelFile(
                    file_name="other-model.th",
                    sha256=hashlib.sha256(other_bytes).hexdigest(),
                    source="download",
                    urls=("https://example.invalid/other.th",),
                ),
            ),
        )
        registry = dict(make_registry())
        registry["other-model"] = other_entry

        first = self.ensure(registry=registry, opener=FakeOpener())
        second = ensure_model(
            "other-model",
            cache_root=self.cache_root,
            registry=registry,
            opener=FakeOpener(payload=other_bytes),
        )

        self.assertNotEqual(first, second)
        self.assertTrue((first / "fake-model.th").is_file())
        self.assertTrue((second / "other-model.th").is_file())
        # The first profile's verified cache is left untouched by the second acquisition.
        self.assertTrue((first / "fake-model.th").is_file())

    def test_on_progress_reports_streamed_bytes(self):
        seen = []
        self.ensure(on_progress=lambda name, done, total: seen.append((name, done, total)))

        self.assertTrue(seen)
        self.assertEqual("fake-model.th", seen[0][0])
        self.assertEqual(len(WEIGHT_BYTES), seen[-1][1])


class ModelManagerBundledFileTests(unittest.TestCase):
    """D7: bag `.yaml` files are copied from a vendored directory, not downloaded."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp.name) / "models"
        self.bundled_root = Path(self.temp.name) / "vendored-remote"
        self.bundled_root.mkdir(parents=True)
        self.bag_bytes = b"models: ['deadbeef']\n"
        (self.bundled_root / "fake-model.yaml").write_bytes(self.bag_bytes)

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, *, bag_sha256=None):
        return {
            "fake-model": ModelEntry(
                model_name="fake-model",
                files=(
                    ModelFile(
                        file_name="fake-model.th",
                        sha256=WEIGHT_SHA256,
                        source="download",
                        urls=("https://example.invalid/weights.th",),
                    ),
                    ModelFile(
                        file_name="fake-model.yaml",
                        sha256=bag_sha256 or hashlib.sha256(self.bag_bytes).hexdigest(),
                        source="bundled",
                    ),
                ),
            )
        }

    def test_bundled_bag_is_copied_and_verified_without_a_download(self):
        opener = FakeOpener()

        result = ensure_model(
            "fake-model",
            cache_root=self.cache_root,
            registry=self.registry(),
            opener=opener,
            bundled_root=self.bundled_root,
        )

        self.assertTrue((result / "fake-model.yaml").is_file())
        self.assertEqual(self.bag_bytes, (result / "fake-model.yaml").read_bytes())
        # Only the .th file is fetched over the network; the bag never is.
        self.assertEqual(1, len(opener.calls))

    def test_tampered_bundled_bag_fails_closed(self):
        with self.assertRaises(ModelAcquisitionError) as caught:
            ensure_model(
                "fake-model",
                cache_root=self.cache_root,
                registry=self.registry(bag_sha256="0" * 64),
                opener=FakeOpener(),
                bundled_root=self.bundled_root,
            )

        self.assertEqual("model.hash_mismatch", caught.exception.code)
        self.assertFalse((self.cache_root / "fake-model").exists())


class RegisteredModelsRealShapeTests(unittest.TestCase):
    """The shipped manifest must be exactly the two-file bag shape D7 assumes."""

    def test_htdemucs_and_htdemucs_6s_each_have_one_weight_and_one_bag(self):
        from SeparationWorker.model_manifest import REGISTERED_MODELS

        for name in ("htdemucs", "htdemucs_6s"):
            entry = REGISTERED_MODELS[name]
            sources = sorted(file.source for file in entry.files)
            self.assertEqual(["bundled", "download"], sources)


class CommandRepoArgumentTests(unittest.TestCase):
    """`_command()` MUST always pass `--repo <dir>` so `LocalRepo` is the only reachable repo."""

    def test_cpu_and_cuda_commands_both_carry_repo(self):
        from SeparationWorker.demucs_adapter import _command

        with tempfile.TemporaryDirectory() as root:
            repo_dir = Path(root) / "models" / "htdemucs"
            repo_dir.mkdir(parents=True)

            cpu_command = _command(Path("song.mp3"), Path("staging"), "cpu", repo=repo_dir)
            cuda_command = _command(Path("song.mp3"), Path("staging"), "cuda", repo=repo_dir)

        for command in (cpu_command, cuda_command):
            self.assertIn("--repo", command)
            self.assertEqual(str(repo_dir), command[command.index("--repo") + 1])


if __name__ == "__main__":
    unittest.main()
