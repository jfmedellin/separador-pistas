"""Headless end-to-end coverage for the temp-stems auto-mixer-export lifecycle.

These tests exercise SeparationController and MixerController together with
the real stem_cache module (no Tk, no gui.py) to prove the two safety
properties that make automatic temp-cache cleanup safe:

* An auto-opened separation result is tracked and discarded on close.
* A manually loaded folder is never tracked and therefore survives close.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from SeparationWorker.demucs_adapter import separate_audio
from SeparationWorker.engine import stem_cache
from SeparationWorker.engine.pcm import PlanarPCM
from SeparationWorker.engine.role_metrics import RoleThresholds
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE, MANIFEST_NAME, METAL_PROFILE
from SeparationWorker.engine.stem_session import STEM_NAMES, StemSession
from SeparationWorker.engine.wav import encode_float32_wav
from SeparationWorker.gui_controller import SeparationController
from SeparationWorker.history import HistoryStore, SplitLibraryController
from SeparationWorker.mixer_controller import MixerController

METAL_SAMPLE_RATE = 44_100
THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2]
    / "Compliance"
    / "evidence"
    / "metal-guitar"
    / "thresholds.json"
)


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

            def separate(_input_file, result_directory, **_options):
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

            def separate(_input_file, result_directory, **_options):
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


def _metal_tone(frequency, amplitude=0.4, seconds=0.25):
    frames = int(METAL_SAMPLE_RATE * seconds)
    time = np.arange(frames, dtype=np.float32) / METAL_SAMPLE_RATE
    wave = (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def _calibrated_thresholds():
    payload = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    payload["calibrated"] = True
    return RoleThresholds.from_payload(payload)


class SixLaneLifecycleTests(unittest.TestCase):
    """Prove a Metal result travels from separation through to a six-lane mixer."""

    def setUp(self):
        self.profile = replace(METAL_PROFILE, specialist_id="metal-lead-rhythm-v1", enabled=True)
        self.lead = _metal_tone(880.0)
        self.rhythm = _metal_tone(110.0, amplitude=0.5)
        self.sources = {
            "vocals": _metal_tone(440.0),
            "drums": _metal_tone(150.0),
            "bass": _metal_tone(80.0),
            "guitar": self.lead + self.rhythm,
            "piano": _metal_tone(330.0, amplitude=0.2),
            "other": _metal_tone(220.0, amplitude=0.1),
        }

    def runner(self, command):
        command = list(command)
        model = command[command.index("--name") + 1]
        output = Path(command[command.index("--out") + 1])
        source = output / model / Path(command[-1]).stem
        source.mkdir(parents=True)
        for name, samples in self.sources.items():
            sf.write(str(source / f"{name}.wav"), samples, METAL_SAMPLE_RATE, subtype="FLOAT")

    def specialist(self, lead, rhythm):
        def run(_guitar_path, staging):
            sf.write(str(staging / "lead_guitar.wav"), lead, METAL_SAMPLE_RATE, subtype="FLOAT")
            sf.write(str(staging / "rhythm_guitar.wav"), rhythm, METAL_SAMPLE_RATE, subtype="FLOAT")

        return run

    def separate(self, root, *, lead=None, rhythm=None):
        audio_file = Path(root) / "song.mp3"
        audio_file.write_bytes(b"audio")
        return separate_audio(
            audio_file,
            Path(root) / "cache" / "metal-result",
            profile=self.profile,
            runner=self.runner,
            cuda_probe=lambda: False,
            specialist=self.specialist(
                self.lead if lead is None else lead, self.rhythm if rhythm is None else rhythm
            ),
            thresholds=_calibrated_thresholds(),
        )

    def mixer_for(self, folder):
        controller = MixerController(
            session_loader=lambda path: StemSession.load(path, profile=self.profile),
            start_worker=lambda target: target(),
            dispatch=lambda callback: callback(),
        )
        controller.load(folder, title="song")
        return controller

    def test_a_metal_result_loads_as_six_synchronized_lanes(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.separate(root)

            session = StemSession.load(result, profile=self.profile)

            self.assertEqual(self.profile.file_names, session.names)
            self.assertEqual(6, len(session.paths))
            self.assertEqual(METAL_SAMPLE_RATE, session.sample_rate)
            self.assertEqual(2, session.channels)
            self.assertEqual((), session.absent)
            self.assertTrue((result / MANIFEST_NAME).is_file())

    def test_the_mixer_exposes_every_lane_of_a_metal_result(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.separate(root)

            controller = self.mixer_for(result)
            try:
                state = controller.state

                self.assertEqual("ready", state.phase)
                self.assertEqual(self.profile.file_names, state.lane_names)
                self.assertTrue(controller.set_muted("lead_guitar.wav", True))
                self.assertTrue(controller.set_solo("rhythm_guitar.wav", True))
            finally:
                controller.close()

    def test_a_track_without_lead_reaches_the_mixer_as_a_labeled_silent_lane(self):
        with tempfile.TemporaryDirectory() as root:
            self.sources["guitar"] = self.rhythm
            silence = np.zeros_like(self.rhythm)

            result = self.separate(root, lead=silence)
            controller = self.mixer_for(result)
            try:
                state = controller.state

                self.assertEqual(("lead_guitar",), state.absent_lanes)
                self.assertTrue(state.is_absent("lead_guitar.wav"))
                self.assertIn("lead_guitar.wav", state.lane_names)
                self.assertTrue((result / "lead_guitar.wav").is_file())
                self.assertTrue(controller.set_muted("lead_guitar.wav", True))
            finally:
                controller.close()

    def test_a_legacy_folder_still_loads_as_four_lanes(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "legacy"
            _write_stem_wavs(folder)

            controller = MixerController(
                start_worker=lambda target: target(), dispatch=lambda callback: callback()
            )
            controller.load(folder, title="legacy")
            try:
                self.assertEqual("ready", controller.state.phase)
                self.assertEqual(STEM_NAMES, controller.state.lane_names)
                self.assertEqual((), controller.state.absent_lanes)
            finally:
                controller.close()


class Arc02InputCopyPublicationTests(unittest.TestCase):
    """D10 regression: the ARC-02 input copy must never live under library_root.

    Exercises SplitLibraryController with the real separate_audio -- and
    therefore the real publish_atomic -- to prove the input copy staged
    under local_data_root()/inputs never confuses publish_atomic's
    exists-path reconciliation short-circuit for library_root/{track_id}.
    """

    def runner(self, command):
        command = list(command)
        model = command[command.index("--name") + 1]
        output = Path(command[command.index("--out") + 1])
        source = output / model / Path(command[-1]).stem
        source.mkdir(parents=True)
        for name in LEGACY_PROFILE.raw_outputs:
            pcm = PlanarPCM(8_000, ((0.1, -0.1, 0.05, -0.05),))
            (source / f"{name}.wav").write_bytes(encode_float32_wav(pcm))

    def test_publish_atomic_succeeds_with_the_copy_staged_outside_library_root(self):
        with tempfile.TemporaryDirectory() as workspace:
            workspace = Path(workspace)
            library_root = workspace / "library"
            appdata = workspace / "appdata"
            inputs_root = appdata / "inputs"
            source = workspace / "song.mp3"
            source.write_bytes(b"original audio")

            store = HistoryStore(workspace / "library.db", library_root)
            staged_inputs = []

            def separate(input_path, result_directory, **_options):
                staged_inputs.append(Path(input_path))
                return separate_audio(
                    input_path,
                    result_directory,
                    profile=LEGACY_PROFILE,
                    runner=self.runner,
                    cuda_probe=lambda: False,
                )

            with mock.patch(
                "SeparationWorker.history.local_data_root", return_value=appdata
            ):
                controller = SplitLibraryController(
                    store,
                    separate=separate,
                    start_worker=lambda target: target(),
                    dispatch=lambda callback: callback(),
                )
                record = controller.add(source, LEGACY_PROFILE.profile_id)

            ready = store.get(record.track_id)
            self.assertEqual("ready", ready.status)
            self.assertTrue(ready.result_directory.is_dir())
            self.assertEqual(library_root, ready.result_directory.parent)
            self.assertEqual(1, len(staged_inputs))
            self.assertTrue(staged_inputs[0].is_relative_to(inputs_root))
            self.assertFalse(staged_inputs[0].is_relative_to(library_root))
            self.assertFalse((inputs_root / record.track_id).exists())


if __name__ == "__main__":
    unittest.main()
