import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from SeparationWorker.demucs_adapter import (
    MODEL_NAME,
    STEM_NAMES,
    WORKER_EXECUTABLE,
    DemucsSeparationError,
    _command,
    _cpu_jobs,
    run_demucs,
    separate_audio,
)
from SeparationWorker import model_manager
from SeparationWorker.model_manager import ModelAcquisitionError
from SeparationWorker.engine.publication import PublicationError
from SeparationWorker.engine.role_metrics import RoleThresholds
from SeparationWorker.engine.stem_cache import cache_key
from SeparationWorker.engine.stem_profile import (
    LEGACY_PROFILE,
    MANIFEST_NAME,
    METAL_PROFILE,
    METAL_STEREO_PROFILE,
    parse_manifest,
)

SAMPLE_RATE = 44100
THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2] / "Compliance" / "evidence" / "metal-guitar" / "thresholds.json"
)
FAKE_REPO_DIR = Path("fake-verified-model-repo")

# SEC-01: separate_audio() now calls model_manager.ensure_model() before every
# run. None of the tests in this module exercise model acquisition itself
# (that lives in test_model_manager.py), so acquisition is stubbed out for
# the whole module -- otherwise every test here would attempt a real network
# download the first time it runs.
_ensure_model_patcher = patch(
    "SeparationWorker.demucs_adapter.model_manager.ensure_model",
    return_value=FAKE_REPO_DIR,
)


def setUpModule():
    _ensure_model_patcher.start()


def tearDownModule():
    _ensure_model_patcher.stop()


class FakeDemucs:
    def __init__(self, *, fail_devices=(), missing=()):
        self.fail_devices = set(fail_devices)
        self.missing = set(missing)
        self.commands = []

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        device = command[command.index("--device") + 1]
        output = Path(command[command.index("--out") + 1])
        audio_file = Path(command[-1])
        if device in self.fail_devices:
            partial = output / "partial.txt"
            partial.write_text("must be removed before fallback", encoding="utf-8")
            raise subprocess.CalledProcessError(7, command)
        source = output / MODEL_NAME / audio_file.stem
        source.mkdir(parents=True)
        for name in STEM_NAMES:
            if name not in self.missing:
                (source / name).write_bytes(f"{device}:{name}".encode("ascii"))
        (source / "unexpected.txt").write_text("not published", encoding="utf-8")


class DemucsAdapterTests(unittest.TestCase):
    def make_paths(self, root, name="song.mix.mp3"):
        audio_file = Path(root) / name
        audio_file.write_bytes(b"audio")
        return audio_file, Path(root) / "exports" / "song-stems"

    def test_real_runner_captures_combined_process_diagnostics(self):
        command = ["python", "-m", "demucs.separate"]
        with patch("SeparationWorker.demucs_adapter._require_demucs_41"):
            with patch("SeparationWorker.demucs_adapter.subprocess.run") as process_run:
                run_demucs(command)

        process_run.assert_called_once_with(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_real_runner_coordinates_cpu_worker_threads(self):
        command = [
            "python",
            "-m",
            "demucs.separate",
            "--device",
            "cpu",
            "--jobs",
            "2",
        ]
        with patch("SeparationWorker.demucs_adapter._require_demucs_41"):
            with patch("SeparationWorker.demucs_adapter._cpu_thread_limit", return_value=4):
                with patch("SeparationWorker.demucs_adapter.subprocess.run") as process_run:
                    run_demucs(command)

        environment = process_run.call_args.kwargs["env"]
        self.assertEqual("4", environment["OMP_NUM_THREADS"])
        self.assertEqual("4", environment["MKL_NUM_THREADS"])

    def test_cpu_parallelism_is_reserved_for_capable_machines(self):
        self.assertEqual(0, _cpu_jobs(4))
        self.assertEqual(2, _cpu_jobs(8))

    def test_development_command_runs_demucs_as_a_python_module(self):
        with patch("SeparationWorker.demucs_adapter.sys.frozen", False, create=True):
            with patch("SeparationWorker.demucs_adapter.os.cpu_count", return_value=16):
                command = _command(Path("song.mp3"), Path("staging"), "cpu", repo=FAKE_REPO_DIR)

        self.assertEqual([sys.executable, "-m", "demucs.separate"], command[:3])
        self.assertEqual("2", command[command.index("--jobs") + 1])
        self.assertEqual("0.1", command[command.index("--overlap") + 1])
        self.assertEqual(str(FAKE_REPO_DIR), command[command.index("--repo") + 1])

    def test_cpu_tuning_applies_to_every_profile_not_only_the_legacy_one(self):
        """The model comes from the profile; the CPU tuning must not follow it.

        These two arrived from different branches and meet in one command, so
        nothing else proves a six-source profile still gets the tuning.
        """
        with patch("SeparationWorker.demucs_adapter.sys.frozen", False, create=True):
            with patch("SeparationWorker.demucs_adapter.os.cpu_count", return_value=16):
                command = _command(Path("song.mp3"), Path("staging"), "cpu", METAL_PROFILE, repo=FAKE_REPO_DIR)

        self.assertEqual(METAL_PROFILE.primary_model, command[command.index("--name") + 1])
        self.assertNotEqual(MODEL_NAME, METAL_PROFILE.primary_model)
        self.assertEqual("2", command[command.index("--jobs") + 1])
        self.assertEqual("0.1", command[command.index("--overlap") + 1])

    def test_cuda_command_keeps_demucs_quality_defaults(self):
        with patch("SeparationWorker.demucs_adapter.sys.frozen", False, create=True):
            command = _command(Path("song.mp3"), Path("staging"), "cuda", repo=FAKE_REPO_DIR)

        self.assertNotIn("--jobs", command)
        self.assertNotIn("--overlap", command)

    def test_frozen_command_runs_the_sibling_worker_executable(self):
        with tempfile.TemporaryDirectory() as root:
            gui = Path(root) / "Stemslayer.exe"
            worker = Path(root) / WORKER_EXECUTABLE
            gui.write_bytes(b"gui")
            worker.write_bytes(b"worker")
            with patch("SeparationWorker.demucs_adapter.sys.executable", str(gui)):
                with patch("SeparationWorker.demucs_adapter.sys.frozen", True, create=True):
                    command = _command(Path("song.mp3"), Path("staging"), "cpu", repo=FAKE_REPO_DIR)

        self.assertEqual(str(worker.resolve()), command[0])
        self.assertEqual(MODEL_NAME, command[command.index("--name") + 1])
        self.assertEqual("cpu", command[command.index("--device") + 1])
        self.assertEqual(str(_cpu_jobs()), command[command.index("--jobs") + 1])
        self.assertEqual("0.1", command[command.index("--overlap") + 1])

    def test_frozen_command_fails_actionably_when_worker_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            gui = Path(root) / "Stemslayer.exe"
            gui.write_bytes(b"gui")
            with patch("SeparationWorker.demucs_adapter.sys.executable", str(gui)):
                with patch("SeparationWorker.demucs_adapter.sys.frozen", True, create=True):
                    with self.assertRaises(DemucsSeparationError) as caught:
                        _command(Path("song.mp3"), Path("staging"), "cpu", repo=FAKE_REPO_DIR)

        self.assertEqual("demucs.worker_missing", caught.exception.code)

    def test_cuda_run_uses_htdemucs_and_atomically_publishes_exact_stems(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeDemucs()

            result = separate_audio(audio_file, output, runner=runner, cuda_probe=lambda: True)

            self.assertEqual(output.resolve(), result)
            self.assertEqual(set(STEM_NAMES), {path.name for path in result.iterdir()})
            self.assertTrue(all(path.read_bytes().startswith(b"cuda:") for path in result.iterdir()))
            command = runner.commands[0]
            self.assertEqual(sys.executable, command[0])
            self.assertEqual("demucs.separate", command[2])
            self.assertEqual(MODEL_NAME, command[command.index("--name") + 1])
            self.assertEqual(str(FAKE_REPO_DIR), command[command.index("--repo") + 1])
            self.assertEqual("cuda", command[command.index("--device") + 1])
            self.assertEqual([], list(output.parent.glob(".song-stems.staging-*")))

    def test_reuses_complete_existing_result_without_rerunning_demucs(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            first_runner = FakeDemucs()
            first = separate_audio(audio_file, output, runner=first_runner, cuda_probe=lambda: True)

            def fail_if_called(_command):
                raise AssertionError("Demucs must not run for an existing complete result")

            second = separate_audio(audio_file, output, runner=fail_if_called, cuda_probe=lambda: False)

            self.assertEqual(first, second)
            self.assertEqual(1, len(first_runner.commands))

    def test_reuses_complete_existing_result_at_a_stem_cache_hex_named_destination(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file = Path(root) / "song.mp3"
            audio_file.write_bytes(b"audio")
            output = Path(root) / "cache" / cache_key(audio_file)
            self.assertEqual(64, len(output.name))
            self.assertTrue(all(character in "0123456789abcdef" for character in output.name))
            first_runner = FakeDemucs()
            first = separate_audio(audio_file, output, runner=first_runner, cuda_probe=lambda: True)

            def fail_if_called(_command):
                raise AssertionError("Demucs must not run for an existing complete result")

            second = separate_audio(audio_file, output, runner=fail_if_called, cuda_probe=lambda: False)

            self.assertEqual(first, second)
            self.assertEqual(1, len(first_runner.commands))

    def test_cuda_failure_retries_once_on_clean_cpu_staging(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeDemucs(fail_devices={"cuda"})

            separate_audio(audio_file, output, runner=runner, cuda_probe=lambda: True)

            devices = [command[command.index("--device") + 1] for command in runner.commands]
            self.assertEqual(["cuda", "cpu"], devices)
            self.assertFalse((output / "partial.txt").exists())
            self.assertTrue(all(path.read_bytes().startswith(b"cpu:") for path in output.iterdir()))

    def test_unavailable_cuda_runs_cpu_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeDemucs()

            separate_audio(audio_file, output, runner=runner, cuda_probe=lambda: False)

            self.assertEqual("cpu", runner.commands[0][runner.commands[0].index("--device") + 1])
            self.assertEqual(1, len(runner.commands))

    def test_incomplete_demucs_output_is_never_published(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                separate_audio(
                    audio_file,
                    output,
                    runner=FakeDemucs(missing={"other.wav"}),
                    cuda_probe=lambda: False,
                )

            self.assertEqual("demucs.output_incomplete", caught.exception.code)
            self.assertFalse(output.exists())

    def test_cpu_process_failure_is_reported_without_publication(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                separate_audio(
                    audio_file,
                    output,
                    runner=FakeDemucs(fail_devices={"cpu"}),
                    cuda_probe=lambda: False,
                )

            self.assertEqual("demucs.inference_failed", caught.exception.code)
            self.assertFalse(output.exists())

    def test_cuda_and_cpu_failure_reports_bounded_diagnostic_tails(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            def failing_runner(command):
                device = command[command.index("--device") + 1]
                output_lines = [f"discarded-{index}" for index in range(20)]
                output_lines.extend([f"{device}-detail-{index}" for index in range(6)])
                raise subprocess.CalledProcessError(7, command, output="\n".join(output_lines))

            with self.assertRaises(DemucsSeparationError) as caught:
                separate_audio(audio_file, output, runner=failing_runner, cuda_probe=lambda: True)

            cause = caught.exception.cause
            self.assertIn("CUDA exited with code 7", cause)
            self.assertIn("cuda-detail-0", cause)
            self.assertIn("CPU fallback CPU exited with code 7", cause)
            self.assertIn("cpu-detail-5", cause)
            self.assertNotIn("discarded-19", cause)
            self.assertLess(len(cause), 1800)

    def test_cpu_failure_without_captured_output_keeps_fake_runners_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                separate_audio(
                    audio_file,
                    output,
                    runner=FakeDemucs(fail_devices={"cpu"}),
                    cuda_probe=lambda: False,
                )

            self.assertEqual("Demucs failed: CPU exited with code 7.", caught.exception.cause)

    def test_missing_input_fails_before_runner_or_output_creation(self):
        with tempfile.TemporaryDirectory() as root:
            runner = FakeDemucs()
            output = Path(root) / "exports" / "result"

            with self.assertRaises(DemucsSeparationError) as caught:
                separate_audio(Path(root) / "missing.wav", output, runner=runner)

            self.assertEqual("input.not_file", caught.exception.code)
            self.assertEqual([], runner.commands)
            self.assertFalse(output.parent.exists())

    def test_model_acquisition_is_requested_for_the_profiles_primary_model(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeDemucs()
            progress = lambda *args: None  # noqa: E731

            separate_audio(audio_file, output, runner=runner, cuda_probe=lambda: True, on_progress=progress)

            model_manager.ensure_model.assert_called_with(MODEL_NAME, on_progress=progress)

    def test_model_acquisition_failure_is_reraised_as_a_demucs_separation_error(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeDemucs()
            failure = ModelAcquisitionError(
                "model.hash_mismatch", "tampered bytes", "retry acquisition"
            )

            with patch(
                "SeparationWorker.demucs_adapter.model_manager.ensure_model", side_effect=failure
            ):
                with self.assertRaises(DemucsSeparationError) as caught:
                    separate_audio(audio_file, output, runner=runner, cuda_probe=lambda: True)

            self.assertEqual("model.hash_mismatch", caught.exception.code)
            self.assertEqual([], runner.commands)
            self.assertFalse(output.exists())


class RunOwnedRegressionTests(unittest.TestCase):
    """D7/D9: a cancelled job must never resurrect the CUDA->CPU fallback,
    and the Popen swap behind run_demucs must not disturb diagnostics."""

    def make_paths(self, root, name="song.mix.mp3"):
        audio_file = Path(root) / name
        audio_file.write_bytes(b"audio")
        return audio_file, Path(root) / "exports" / "song-stems"

    def test_job_cancelled_during_cuda_attempt_never_triggers_the_cpu_fallback(self):
        from SeparationWorker.job_manager import JobCancelled

        calls = {"cuda": 0, "cpu": 0}

        def runner(command):
            device = command[command.index("--device") + 1]
            calls[device] += 1
            if device == "cuda":
                raise JobCancelled("job cancelled mid CUDA attempt")
            raise AssertionError("the CPU fallback must never run after a cancellation")

        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(JobCancelled):
                separate_audio(audio_file, output, runner=runner, cuda_probe=lambda: True)

            self.assertEqual(1, calls["cuda"])
            self.assertEqual(0, calls["cpu"])
            self.assertFalse(output.exists())

    def test_called_process_error_output_survives_the_popen_swap_for_diagnostics(self):
        from SeparationWorker.demucs_adapter import _diagnostic_tail, _failure_cause

        command = [
            sys.executable,
            "-c",
            "import sys; print('boom line one'); print('boom line two'); sys.exit(9)",
        ]
        with patch("SeparationWorker.demucs_adapter._require_demucs_41"):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                run_demucs(command)

        error = caught.exception
        self.assertEqual(9, error.returncode)
        tail = _diagnostic_tail(error)
        self.assertIn("boom line one", tail)
        self.assertIn("boom line two", tail)
        self.assertIn("CPU exited with code 9", _failure_cause("CPU", error))


class DemucsCliTests(unittest.TestCase):
    def test_cli_reports_published_directory(self):
        from SeparationWorker.cli import main

        with patch("SeparationWorker.cli.separate_audio", return_value=Path("result")) as separate:
            with patch("builtins.print") as output:
                exit_code = main(["song.mp3", "stems"])

        self.assertEqual(0, exit_code)
        separate.assert_called_once_with("song.mp3", "stems")
        output.assert_called_once_with(Path("result"))


def tone(frequency, amplitude=0.4, seconds=0.25):
    frames = int(SAMPLE_RATE * seconds)
    time = np.arange(frames, dtype=np.float32) / SAMPLE_RATE
    wave = (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def calibrated_thresholds():
    payload = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    payload["calibrated"] = True
    return RoleThresholds.from_payload(payload)


def admitted_metal(specialist_id="metal-lead-rhythm-v1"):
    return replace(METAL_PROFILE, specialist_id=specialist_id, enabled=True)


class FakeMetalDemucs:
    """Write real six-source audio so folding and role validation are exercised."""

    def __init__(self, sources, *, missing=()):
        self.sources = sources
        self.missing = set(missing)
        self.commands = []

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        model = command[command.index("--name") + 1]
        output = Path(command[command.index("--out") + 1])
        source = output / model / Path(command[-1]).stem
        source.mkdir(parents=True)
        for name, samples in self.sources.items():
            if name in self.missing:
                continue
            sf.write(str(source / f"{name}.wav"), samples, SAMPLE_RATE, subtype="FLOAT")


def specialist_writing(lead, rhythm, *, omit=(), rate=SAMPLE_RATE):
    def run(guitar_path, staging):
        for name, samples in (("lead_guitar.wav", lead), ("rhythm_guitar.wav", rhythm)):
            if name in omit:
                continue
            sf.write(str(staging / name), samples, rate, subtype="FLOAT")

    return run


class MetalProfileSeparationTests(unittest.TestCase):
    def setUp(self):
        self.lead_part = tone(880.0)
        self.rhythm_part = tone(110.0, amplitude=0.5)
        self.guitar = self.lead_part + self.rhythm_part
        self.piano = tone(330.0, amplitude=0.2)
        self.other = tone(220.0, amplitude=0.1)
        self.sources = {
            "vocals": tone(440.0),
            "drums": tone(150.0),
            "bass": tone(80.0),
            "guitar": self.guitar,
            "piano": self.piano,
            "other": self.other,
        }

    def make_paths(self, root):
        audio_file = Path(root) / "song.mp3"
        audio_file.write_bytes(b"audio")
        return audio_file, Path(root) / "cache" / "metal-result"

    def separate(self, audio_file, output, **overrides):
        settings = {
            "profile": admitted_metal(),
            "runner": FakeMetalDemucs(self.sources),
            "cuda_probe": lambda: False,
            "specialist": specialist_writing(self.lead_part, self.rhythm_part),
            "thresholds": calibrated_thresholds(),
        }
        settings.update(overrides)
        return separate_audio(audio_file, output, **settings)

    def test_disabled_metal_profile_refuses_before_running_anything(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeMetalDemucs(self.sources)

            with self.assertRaises(DemucsSeparationError) as caught:
                separate_audio(audio_file, output, profile=METAL_PROFILE, runner=runner)

            self.assertEqual("profile.disabled", caught.exception.code)
            self.assertEqual([], runner.commands)
            self.assertFalse(output.parent.exists())

    def test_profile_requiring_a_specialist_refuses_without_one(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(audio_file, output, specialist=None)

            self.assertEqual("profile.specialist_missing", caught.exception.code)
            self.assertFalse(output.exists())

    def test_uncalibrated_thresholds_block_the_profile(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            shipped = RoleThresholds.load(THRESHOLDS_PATH)
            self.assertFalse(shipped.calibrated)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(audio_file, output, thresholds=shipped)

            self.assertEqual("profile.thresholds_uncalibrated", caught.exception.code)
            self.assertFalse(output.exists())

    def test_metal_publishes_six_ordered_lanes_with_a_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            runner = FakeMetalDemucs(self.sources)

            result = self.separate(audio_file, output, runner=runner)

            published = {path.name for path in result.iterdir()}
            self.assertEqual(set(admitted_metal().file_names) | {MANIFEST_NAME}, published)
            self.assertEqual("htdemucs_6s", runner.commands[0][runner.commands[0].index("--name") + 1])
            manifest = parse_manifest((result / MANIFEST_NAME).read_bytes())
            self.assertEqual(admitted_metal().file_names, manifest.file_names)
            self.assertEqual((), manifest.absent_lane_ids)

    def test_combined_guitar_never_appears_beside_its_children(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            result = self.separate(audio_file, output)

            self.assertFalse((result / "guitar.wav").exists())
            self.assertTrue((result / "lead_guitar.wav").is_file())
            self.assertTrue((result / "rhythm_guitar.wav").is_file())

    def test_residual_folds_piano_and_primary_other_without_duplicating_guitar(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            result = self.separate(audio_file, output)

            published, rate = sf.read(str(result / "other.wav"), dtype="float32", always_2d=True)
            self.assertEqual(SAMPLE_RATE, rate)
            np.testing.assert_allclose(self.piano + self.other, published, atol=1e-6)

    def test_track_without_lead_publishes_a_declared_silent_lane(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            sources = dict(self.sources, guitar=self.rhythm_part)
            silence = np.zeros_like(self.rhythm_part)

            result = self.separate(
                audio_file,
                output,
                runner=FakeMetalDemucs(sources),
                specialist=specialist_writing(silence, self.rhythm_part),
            )

            manifest = parse_manifest((result / MANIFEST_NAME).read_bytes())
            self.assertEqual(("lead_guitar",), manifest.absent_lane_ids)
            self.assertTrue((result / "lead_guitar.wav").is_file())
            lead = next(lane for lane in manifest.lanes if lane.lane_id == "lead_guitar")
            self.assertIn("reconstruct", lead.absence_reason)

    def test_silent_lane_that_loses_energy_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            silence = np.zeros_like(self.lead_part)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(
                    audio_file, output, specialist=specialist_writing(silence, self.rhythm_part)
                )

            self.assertEqual("specialist.reconstruction_failed", caught.exception.code)
            self.assertFalse(output.exists())

    def test_noise_floor_placeholder_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            placeholder = tone(880.0, amplitude=1e-3)
            sources = dict(self.sources, guitar=placeholder + self.rhythm_part)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(
                    audio_file,
                    output,
                    runner=FakeMetalDemucs(sources),
                    specialist=specialist_writing(placeholder, self.rhythm_part),
                )

            self.assertEqual("specialist.lane_not_audible", caught.exception.code)
            self.assertFalse(output.exists())

    def test_duplicated_guitar_energy_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(
                    audio_file, output, specialist=specialist_writing(self.guitar, self.guitar)
                )

            self.assertEqual("specialist.reconstruction_failed", caught.exception.code)
            self.assertFalse(output.exists())

    def test_misaligned_role_lane_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            short_lead = self.lead_part[: len(self.lead_part) // 2]

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(
                    audio_file, output, specialist=specialist_writing(short_lead, self.rhythm_part)
                )

            self.assertEqual("specialist.misaligned", caught.exception.code)
            self.assertFalse(output.exists())

    def test_partial_specialist_output_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(
                    audio_file,
                    output,
                    specialist=specialist_writing(
                        self.lead_part, self.rhythm_part, omit=("rhythm_guitar.wav",)
                    ),
                )

            self.assertEqual("specialist.output_incomplete", caught.exception.code)
            self.assertIn("rhythm_guitar.wav", caught.exception.cause)
            self.assertFalse(output.exists())

    def test_missing_primary_source_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(
                    audio_file, output, runner=FakeMetalDemucs(self.sources, missing={"piano"})
                )

            self.assertEqual("demucs.output_incomplete", caught.exception.code)
            self.assertIn("piano.wav", caught.exception.cause)
            self.assertFalse(output.exists())


class ProfileResultReuseTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "vocals": tone(440.0),
            "drums": tone(150.0),
            "bass": tone(80.0),
            "guitar": tone(880.0) + tone(110.0, amplitude=0.5),
            "piano": tone(330.0, amplitude=0.2),
            "other": tone(220.0, amplitude=0.1),
        }
        self.lead = tone(880.0)
        self.rhythm = tone(110.0, amplitude=0.5)

    def separate(self, audio_file, output, profile, runner):
        return separate_audio(
            audio_file,
            output,
            profile=profile,
            runner=runner,
            cuda_probe=lambda: False,
            specialist=specialist_writing(self.lead, self.rhythm),
            thresholds=calibrated_thresholds(),
        )

    def test_metal_result_is_reused_without_rerunning_the_pipeline(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file = Path(root) / "song.mp3"
            audio_file.write_bytes(b"audio")
            output = Path(root) / "cache" / "metal-result"
            first_runner = FakeMetalDemucs(self.sources)
            first = self.separate(audio_file, output, admitted_metal(), first_runner)

            def fail_if_called(_command):
                raise AssertionError("The pipeline must not run for a reusable result")

            second = self.separate(audio_file, output, admitted_metal(), fail_if_called)

            self.assertEqual(first, second)
            self.assertEqual(1, len(first_runner.commands))

    def test_a_result_from_another_specialist_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file = Path(root) / "song.mp3"
            audio_file.write_bytes(b"audio")
            output = Path(root) / "cache" / "metal-result"
            self.separate(audio_file, output, admitted_metal("v1"), FakeMetalDemucs(self.sources))
            second_runner = FakeMetalDemucs(self.sources)

            with self.assertRaises(PublicationError) as caught:
                self.separate(audio_file, output, admitted_metal("v2"), second_runner)

            self.assertEqual("publication.reconciliation_required", caught.exception.code)
            self.assertEqual(1, len(second_runner.commands))

    def test_a_manifest_less_legacy_folder_is_never_adopted_by_metal(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file = Path(root) / "song.mp3"
            audio_file.write_bytes(b"audio")
            output = Path(root) / "cache" / "result"
            output.mkdir(parents=True)
            for name in LEGACY_PROFILE.file_names:
                sf.write(str(output / name), tone(440.0), SAMPLE_RATE, subtype="FLOAT")

            with self.assertRaises(PublicationError) as caught:
                self.separate(audio_file, output, admitted_metal(), FakeMetalDemucs(self.sources))

            self.assertEqual("publication.reconciliation_required", caught.exception.code)
            self.assertEqual(
                set(LEGACY_PROFILE.file_names), {path.name for path in output.iterdir()}
            )


def mono_tone(frequency, amplitude=0.4, seconds=0.25):
    frames = int(SAMPLE_RATE * seconds)
    time = np.arange(frames, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)


def panned(left, right):
    return np.stack([left, right], axis=1).astype(np.float32)


class MetalStereoSeparationTests(unittest.TestCase):
    """The deterministic split runs through the same gates as a specialist."""

    def setUp(self):
        self.solo = mono_tone(880.0, amplitude=0.4)
        self.rhythm = mono_tone(110.0, amplitude=0.5)
        # Conventional metal placement: solo centred, rhythm doubled hard.
        self.guitar = panned(self.solo + self.rhythm, self.solo - self.rhythm)
        self.piano = tone(330.0, amplitude=0.2)
        self.other = tone(220.0, amplitude=0.1)
        self.sources = {
            "vocals": tone(440.0),
            "drums": tone(150.0),
            "bass": tone(80.0),
            "guitar": self.guitar,
            "piano": self.piano,
            "other": self.other,
        }

    def make_paths(self, root):
        audio_file = Path(root) / "song.mp3"
        audio_file.write_bytes(b"audio")
        return audio_file, Path(root) / "cache" / "stereo-result"

    def separate(self, audio_file, output, **overrides):
        settings = {
            "profile": METAL_STEREO_PROFILE,
            "runner": FakeMetalDemucs(self.sources),
            "cuda_probe": lambda: False,
        }
        settings.update(overrides)
        return separate_audio(audio_file, output, **settings)

    def test_it_runs_without_a_specialist_or_calibrated_thresholds(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            result = self.separate(audio_file, output)

            published = {path.name for path in result.iterdir()}
            self.assertEqual(set(METAL_STEREO_PROFILE.file_names) | {MANIFEST_NAME}, published)

    def test_the_centred_solo_and_panned_rhythm_land_in_their_own_lanes(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            result = self.separate(audio_file, output)

            center, _rate = sf.read(str(result / "guitar_center.wav"), dtype="float32", always_2d=True)
            sides, _rate = sf.read(str(result / "guitar_sides.wav"), dtype="float32", always_2d=True)
            np.testing.assert_allclose(panned(self.solo, self.solo), center, atol=1e-5)
            np.testing.assert_allclose(panned(self.rhythm, -self.rhythm), sides, atol=1e-5)

    def test_the_parts_reconstruct_the_isolated_guitar(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            result = self.separate(audio_file, output)

            center, _r = sf.read(str(result / "guitar_center.wav"), dtype="float32", always_2d=True)
            sides, _r = sf.read(str(result / "guitar_sides.wav"), dtype="float32", always_2d=True)
            np.testing.assert_allclose(self.guitar, center + sides, atol=1e-5)

    def test_a_fully_centred_guitar_declares_the_sides_absent(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            centred = dict(self.sources, guitar=panned(self.solo, self.solo))

            result = self.separate(audio_file, output, runner=FakeMetalDemucs(centred))

            manifest = parse_manifest((result / MANIFEST_NAME).read_bytes())
            self.assertEqual(("guitar_sides",), manifest.absent_lane_ids)
            self.assertTrue((result / "guitar_sides.wav").is_file())

    def test_the_residual_still_folds_piano_and_primary_other(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)

            result = self.separate(audio_file, output)

            published, _rate = sf.read(str(result / "other.wav"), dtype="float32", always_2d=True)
            np.testing.assert_allclose(self.piano + self.other, published, atol=1e-6)

    def test_an_unregistered_splitter_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            profile = replace(METAL_STEREO_PROFILE, splitter_id="center-sides-v99")

            with self.assertRaises(DemucsSeparationError) as caught:
                self.separate(audio_file, output, profile=profile)

            self.assertEqual("profile.splitter_unregistered", caught.exception.code)
            self.assertFalse(output.exists())

    def test_a_stereo_result_is_never_reused_for_the_role_profile(self):
        with tempfile.TemporaryDirectory() as root:
            audio_file, output = self.make_paths(root)
            self.separate(audio_file, output)

            with self.assertRaises(PublicationError) as caught:
                separate_audio(
                    audio_file,
                    output,
                    profile=admitted_metal(),
                    runner=FakeMetalDemucs(self.sources),
                    cuda_probe=lambda: False,
                    specialist=specialist_writing(
                        panned(self.solo, self.solo), panned(self.rhythm, -self.rhythm)
                    ),
                    thresholds=calibrated_thresholds(),
                )

            self.assertEqual("publication.reconciliation_required", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
