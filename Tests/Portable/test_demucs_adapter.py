import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from SeparationWorker.engine.stem_cache import cache_key


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
                command = _command(Path("song.mp3"), Path("staging"), "cpu")

        self.assertEqual([sys.executable, "-m", "demucs.separate"], command[:3])
        self.assertEqual("2", command[command.index("--jobs") + 1])
        self.assertEqual("0.1", command[command.index("--overlap") + 1])

    def test_cuda_command_keeps_demucs_quality_defaults(self):
        with patch("SeparationWorker.demucs_adapter.sys.frozen", False, create=True):
            command = _command(Path("song.mp3"), Path("staging"), "cuda")

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
                    command = _command(Path("song.mp3"), Path("staging"), "cpu")

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
                        _command(Path("song.mp3"), Path("staging"), "cpu")

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


class DemucsCliTests(unittest.TestCase):
    def test_cli_reports_published_directory(self):
        from SeparationWorker.cli import main

        with patch("SeparationWorker.cli.separate_audio", return_value=Path("result")) as separate:
            with patch("builtins.print") as output:
                exit_code = main(["song.mp3", "stems"])

        self.assertEqual(0, exit_code)
        separate.assert_called_once_with("song.mp3", "stems")
        output.assert_called_once_with(Path("result"))


if __name__ == "__main__":
    unittest.main()
