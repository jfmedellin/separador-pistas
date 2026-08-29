import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from SeparationWorker.demucs_adapter import (
    MODEL_NAME,
    STEM_NAMES,
    DemucsSeparationError,
    separate_audio,
)


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
