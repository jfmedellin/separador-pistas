"""Headless end-to-end coverage for the temp-stems auto-mixer-export lifecycle.

These tests exercise SeparationController and MixerController together with
the real stem_cache module (no Tk, no gui.py) to prove the two safety
properties that make automatic temp-cache cleanup safe:

* An auto-opened separation result is tracked and discarded on close.
* A manually loaded folder is never tracked and therefore survives close.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from SeparationWorker.engine import stem_cache
from SeparationWorker.engine.pcm import PlanarPCM
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.engine.wav import encode_float32_wav
from SeparationWorker.gui_controller import SeparationController
from SeparationWorker.mixer_controller import MixerController


def _write_stem_wavs(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(STEM_NAMES):
        pcm = PlanarPCM(8_000, ((0.1 * (index + 1), -0.1 * (index + 1), 0.05, -0.05),))
        (folder / name).write_bytes(encode_float32_wav(pcm))


class HeadlessApp:
    """Minimal reproduction of gui.py's cache-tracking orchestration.

    Mirrors StemslayerApp exactly on the two properties under test: only the
    auto-open path (a completed separation) appends to ``cache_directories``,
    and close() discards those tracked directories only after the mixer
    controller has released its playback resources.
    """

    def __init__(self, *, separate):
        self.cache_directories: list[Path] = []
        self.mixer_controller = MixerController(
            dispatch=lambda callback: callback(),
            start_worker=lambda target: target(),
        )
        self.separation_controller = SeparationController(
            separate=separate,
            start_worker=lambda target: target(),
            dispatch=lambda callback: callback(),
            on_success=self._separation_succeeded,
        )

    def _separation_succeeded(self, result: Path) -> None:
        self.cache_directories.append(Path(result))
        input_file = self.separation_controller.state.input_file
        title = Path(input_file).stem if input_file else ""
        self.mixer_controller.load(result, title=title)

    def load_manually(self, folder: Path) -> None:
        self.mixer_controller.load(folder, title=Path(folder).name)

    def close(self) -> None:
        self.mixer_controller.close()
        for directory in self.cache_directories:
            stem_cache.discard(directory)


class AutoOpenedResultDiscardTests(unittest.TestCase):
    def test_separate_then_auto_open_then_export_then_close_removes_the_cache_dir(self):
        with tempfile.TemporaryDirectory() as isolated_root, tempfile.TemporaryDirectory() as workspace:
            isolated_root = Path(isolated_root) / "cache"
            export_destination = Path(workspace) / "export"
            export_destination.mkdir()
            input_file = Path(workspace) / "song.mp3"
            input_file.write_bytes(b"audio")

            def separate(_input_file, result_directory):
                _write_stem_wavs(Path(result_directory))
                return Path(result_directory)

            with mock.patch("SeparationWorker.engine.stem_cache.cache_root", return_value=isolated_root):
                app = HeadlessApp(separate=separate)
                app.separation_controller.set_input_file(str(input_file))

                self.assertTrue(app.separation_controller.start())

                self.assertEqual(1, len(app.cache_directories))
                cache_dir = app.cache_directories[0]
                self.assertTrue(cache_dir.is_dir())
                self.assertEqual(isolated_root, cache_dir.parent)
                self.assertEqual("ready", app.mixer_controller.state.phase)
                self.assertEqual("song", app.mixer_controller.state.title)

                written = app.mixer_controller.export_stem("vocals.wav", export_destination)
                self.assertIsNotNone(written)
                self.assertEqual(
                    (cache_dir / "vocals.wav").read_bytes(),
                    written.read_bytes(),
                )

                app.close()

                self.assertFalse(cache_dir.exists())


class ManualLoadPreservationTests(unittest.TestCase):
    def test_manual_load_then_export_then_close_leaves_the_folder_intact(self):
        with tempfile.TemporaryDirectory() as workspace:
            manual_folder = Path(workspace) / "my-manual-stems"
            _write_stem_wavs(manual_folder)
            export_destination = Path(workspace) / "export"
            export_destination.mkdir()

            def separate(_input_file, result_directory):
                raise AssertionError("separation must not run for a manual-only session")

            app = HeadlessApp(separate=separate)
            app.load_manually(manual_folder)

            self.assertEqual([], app.cache_directories)
            self.assertEqual("ready", app.mixer_controller.state.phase)
            self.assertEqual("my-manual-stems", app.mixer_controller.state.title)

            written = app.mixer_controller.export_stem("drums.wav", export_destination)
            self.assertIsNotNone(written)

            app.close()

            self.assertTrue(manual_folder.is_dir())
            self.assertEqual(set(STEM_NAMES), {path.name for path in manual_folder.iterdir()})


if __name__ == "__main__":
    unittest.main()
