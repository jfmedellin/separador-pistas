import math
import json
import os
import struct
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from SeparationWorker.engine.mixer import (
    MixSetting,
    MixerSnapshot,
    effective_gains,
    gain_from_percent,
    render_mix,
)
from SeparationWorker.engine.fixture_harness import build_synthetic_oracle
from SeparationWorker.engine.pcm import AudioContractError, PlanarPCM, compute_residual
from SeparationWorker.engine.publication import (
    CancellationToken,
    PublicationError,
    expected_publication,
    publish_atomic,
    reconcile_publication,
)
from SeparationWorker.engine.wav import ExportError, encode_float32_wav


def pcm(*channels, rate=48_000, frame_zero=0):
    return PlanarPCM(rate, tuple(tuple(channel) for channel in channels), frame_zero)


class PCMContractTests(unittest.TestCase):
    def assert_audio_error(self, code, operation):
        with self.assertRaises(AudioContractError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertTrue(caught.exception.cause)
        self.assertTrue(caught.exception.recovery)

    def test_rejects_non_finite_and_misaligned_planar_pcm(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.assert_audio_error("pcm.non_finite", lambda: pcm((value,)))
        self.assert_audio_error("pcm.channel_misalignment", lambda: pcm((0.0,), (0.0, 1.0)))
        self.assert_audio_error("pcm.frame_zero", lambda: pcm((0.0,), frame_zero=1))

        source = pcm((0.0, 0.0))
        short = pcm((0.0,))
        self.assert_audio_error(
            "pcm.identity_mismatch",
            lambda: compute_residual(source, short, short, short),
        )

    def test_rejects_malformed_planar_pcm(self):
        cases = ((1, "pcm.planar"), ((1.0,), "pcm.planar"), ((), "pcm.empty"), (((),), "pcm.empty"))
        for planar, code in cases:
            with self.subTest(planar=planar):
                operation = lambda planar=planar: PlanarPCM(48_000, planar)
                self.assert_audio_error(code, operation)

    def test_omits_fixture_negligible_residual_with_calibration_boundary(self):
        source = pcm((0.25, -0.25))
        vocals = pcm((0.24999999, -0.24999999))
        silence = pcm((0.0, 0.0))

        result = compute_residual(
            source,
            vocals,
            silence,
            silence,
            synthetic_negligible_peak=1e-6,
        )

        self.assertIsNone(result.pcm)
        self.assertIn("CALIBRATION REQUIRED", result.omission_reason)

    def test_residual_uses_float64_sum_and_one_float32_rounding(self):
        source = pcm((1.0, -0.5))
        vocals = pcm((0.1, 0.25))
        drums = pcm((0.2, 0.125))
        bass = pcm((0.3, -0.125))

        result = compute_residual(source, vocals, drums, bass)

        expected = tuple(
            struct.unpack("<f", struct.pack("<f", float(s) - (float(v) + float(d) + float(b))))[0]
            for s, v, d, b in zip(
                source.planar[0], vocals.planar[0], drums.planar[0], bass.planar[0]
            )
        )
        self.assertEqual(expected, result.pcm.planar[0])
        for index, sample in enumerate(source.planar[0]):
            reconstructed = float(vocals.planar[0][index]) + float(drums.planar[0][index])
            reconstructed += float(bass.planar[0][index]) + float(result.pcm.planar[0][index])
            self.assertAlmostEqual(sample, reconstructed, places=6)


class MixerAndWavTests(unittest.TestCase):
    def test_effective_gains_apply_volume_mute_and_multiple_solo(self):
        snapshot = MixerSnapshot(
            (
                MixSetting("Vocals", gain=0.25, solo=True),
                MixSetting("Drums", gain=0.5, solo=True),
                MixSetting("Bass", gain=1.0),
                MixSetting("Other", gain=1.0, muted=True, solo=True),
            )
        )

        self.assertEqual(
            {"Vocals": 0.25, "Drums": 0.5, "Bass": 0.0, "Other": 0.0},
            effective_gains(snapshot),
        )

    def test_volume_percent_maps_only_to_attenuation(self):
        self.assertEqual(0.0, gain_from_percent(0))
        self.assertEqual(0.5, gain_from_percent(50))
        self.assertEqual(1.0, gain_from_percent(100))
        for invalid in (-1, 101, math.nan, math.inf, "50"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AudioContractError) as caught:
                    gain_from_percent(invalid)
                self.assertEqual("mixer.invalid_volume", caught.exception.code)

    def test_fixture_oracle_is_deterministic_rights_clear_and_uncalibrated(self):
        first = build_synthetic_oracle()
        second = build_synthetic_oracle()

        self.assertEqual(first, second)
        report = json.loads(first[1])
        self.assertEqual("synthetic-project-generated", report["fixture"])
        self.assertIn("No third-party audio", report["rights"])
        self.assertEqual("CALIBRATION REQUIRED", report["calibration"])

    def test_immutable_snapshot_is_the_export_oracle(self):
        stems = {
            "Vocals": pcm((0.25, 0.5)),
            "Drums": pcm((0.5, 0.25)),
            "Bass": pcm((0.125, 0.125)),
        }
        snapshot = MixerSnapshot(
            (
                MixSetting("Vocals", gain=0.5, solo=True),
                MixSetting("Drums", gain=1.0),
                MixSetting("Bass", gain=1.0, muted=True),
            )
        )

        rendered = render_mix(stems, snapshot)

        self.assertEqual(pcm((0.125, 0.25)), rendered)
        with self.assertRaises(FrozenInstanceError):
            snapshot.settings = ()

    def test_float32_riff_is_fixed_metadata_free_and_interleaved(self):
        audio = pcm((0.25, -0.5), (0.75, -1.0), rate=44_100)

        first = encode_float32_wav(audio)
        second = encode_float32_wav(audio)

        self.assertEqual(first, second)
        self.assertEqual(b"RIFF", first[:4])
        self.assertEqual(b"WAVEfmt ", first[8:16])
        self.assertEqual((3, 2, 44_100, 32), struct.unpack_from("<HHIxxxxxxH", first, 20))
        self.assertEqual(b"data", first[36:40])
        self.assertEqual((0.25, 0.75, -0.5, -1.0), struct.unpack("<4f", first[44:]))
        self.assertNotIn(b"LIST", first)

    def test_clipping_is_blocked_and_peak_is_disclosed(self):
        with self.assertRaises(ExportError) as caught:
            encode_float32_wav(pcm((0.5, -1.25)))
        self.assertEqual("export.clipping", caught.exception.code)
        self.assertEqual(1.25, caught.exception.peak)
        self.assertIn("gain", caught.exception.recovery.lower())


class AtomicPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.destination = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_clean(self):
        self.assertEqual([], list(self.destination.iterdir()))

    @staticmethod
    def snapshot(path):
        return tuple(
            (item.relative_to(path).as_posix(), item.read_bytes() if item.is_file() else None)
            for item in sorted(path.rglob("*"))
        )

    def test_rejects_result_and_file_path_escape(self):
        for result_name, files in (("../escape", {"stem.wav": b"x"}), ("result", {"../stem.wav": b"x"})):
            with self.subTest(result_name=result_name, files=files):
                with self.assertRaises(PublicationError) as caught:
                    publish_atomic(self.destination, result_name, files)
                self.assertEqual("publication.path_escape", caught.exception.code)
                self.assert_clean()

    def test_cancellation_race_cleans_staging_and_publishes_nothing(self):
        token = CancellationToken()

        def cancel_during_validation(_staging):
            token.cancel()

        with self.assertRaises(PublicationError) as caught:
            publish_atomic(
                self.destination,
                "job-1",
                {"Vocals.wav": b"complete"},
                cancellation=token,
                validate=cancel_during_validation,
            )

        self.assertEqual("publication.cancelled", caught.exception.code)
        self.assert_clean()

    def test_atomic_rename_failure_leaves_no_partial_publication(self):
        with patch("SeparationWorker.engine.publication.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(PublicationError) as caught:
                publish_atomic(self.destination, "job-2", {"Vocals.wav": b"complete"})

        self.assertEqual("publication.commit_failed", caught.exception.code)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assert_clean()

    def test_validation_failure_cleans_staging_and_publishes_nothing(self):
        def reject(_staging):
            raise ValueError("injected validation failure")

        with self.assertRaises(PublicationError) as caught:
            publish_atomic(self.destination, "invalid", {"manifest.json": b"{}"}, validate=reject)

        self.assertEqual("publication.stage_failed", caught.exception.code)
        self.assert_clean()

    def test_post_rename_fsync_failure_is_uncertain_and_preserves_result(self):
        files = {"manifest.json": b'{"job":"stable"}', "Vocals.wav": b"complete"}
        expected = expected_publication(files)
        with patch(
            "SeparationWorker.engine.publication._fsync_directory",
            side_effect=(None, OSError("injected destination fsync")),
        ):
            with self.assertRaises(PublicationError) as caught:
                publish_atomic(self.destination, "uncertain", files)

        error = caught.exception
        final = self.destination / "uncertain"
        self.assertEqual("publication.commit_outcome_uncertain", error.code)
        self.assertFalse(error.retryable)
        self.assertEqual("uncertain", error.result_name)
        self.assertEqual(expected.result_identity, error.expected_result_identity)
        self.assertEqual(expected.manifest_digest, error.manifest_digest)
        self.assertEqual(expected, expected_publication(dict(reversed(tuple(files.items())))))
        self.assertIn("reconcile", error.reconciliation_action.lower())
        self.assertEqual(files, {item.name: item.read_bytes() for item in final.iterdir()})
        self.assertEqual([final], list(self.destination.iterdir()))

    def test_reconciliation_adopts_exact_match_and_allows_absent(self):
        files = {"manifest.json": b"{}", "stem.wav": b"complete"}
        absent = reconcile_publication(self.destination, "missing", files)
        self.assertEqual(("absent", True, None), (absent.state, absent.retryable, absent.path))

        final = publish_atomic(self.destination, "existing", files)
        before = self.snapshot(self.destination)
        adopted = reconcile_publication(self.destination, "existing", files)
        self.assertEqual(("adopted", False, final), (adopted.state, adopted.retryable, adopted.path))
        self.assertEqual(expected_publication(files), adopted.expectation)
        self.assertEqual(before, self.snapshot(self.destination))
        self.assertEqual(final, publish_atomic(self.destination, "existing", files))
        self.assertEqual(before, self.snapshot(self.destination))

    def test_reconciliation_blocks_unsafe_presence_without_mutation(self):
        files = {"manifest.json": b"{}", "stem.wav": b"complete"}
        cases = {
            "incomplete": {"manifest.json": b"{}"},
            "mismatch": {"manifest.json": b"{}", "stem.wav": b"changed"},
            "conflict": {**files, "unexpected.bin": b"conflict"},
        }
        for name, present in cases.items():
            final = self.destination / name
            final.mkdir()
            for filename, data in present.items():
                (final / filename).write_bytes(data)
            before = self.snapshot(final)
            with self.assertRaises(PublicationError) as caught:
                reconcile_publication(self.destination, name, files)
            self.assertEqual("publication.reconciliation_required", caught.exception.code)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(before, self.snapshot(final))

        unreadable = self.destination / "unreadable"
        unreadable.mkdir()
        for filename, data in files.items():
            (unreadable / filename).write_bytes(data)
        before = self.snapshot(unreadable)
        original = Path.read_bytes

        def fail_selected(path):
            if unreadable in path.parents:
                raise OSError("injected unreadable result")
            return original(path)

        with patch.object(Path, "read_bytes", fail_selected):
            with self.assertRaises(PublicationError) as caught:
                reconcile_publication(self.destination, "unreadable", files)
        self.assertEqual("publication.reconciliation_required", caught.exception.code)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(before, self.snapshot(unreadable))

    def test_success_renames_one_complete_directory(self):
        published = publish_atomic(
            self.destination,
            "job-3",
            {"Vocals.wav": b"v", "nested/manifest.json": b"{}"},
        )

        self.assertEqual(self.destination / "job-3", published)
        self.assertEqual(b"v", (published / "Vocals.wav").read_bytes())
        self.assertEqual(b"{}", (published / "nested" / "manifest.json").read_bytes())
        self.assertEqual([published], list(self.destination.iterdir()))


if __name__ == "__main__":
    unittest.main()
