import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from SeparationWorker.engine.mixer import MixerSnapshot
from SeparationWorker.mixer_controller import MixerController, _export_target


STEMS = ("vocals.wav", "drums.wav", "bass.wav", "other.wav")


@dataclass(frozen=True)
class FakeSession:
    folder: Path
    frame_count: int = 100
    sample_rate: int = 10
    channels: int = 2

    @property
    def paths(self):
        return tuple(self.folder / name for name in STEMS)


class QueuedExecution:
    def __init__(self):
        self.workers = []
        self.events = []

    def start_worker(self, callback):
        self.workers.append(callback)

    def dispatch(self, callback):
        self.events.append(callback)


class FakePlayback:
    def __init__(self, session, *, on_state, on_error):
        self.session = session
        self.on_state = on_state
        self.on_error = on_error
        self.commands = []
        self.closed = False

    def play(self):
        self.commands.append(("play",))

    def pause(self):
        self.commands.append(("pause",))

    def seek(self, frame):
        self.commands.append(("seek", frame))

    def apply(self, snapshot):
        self.commands.append(("apply", snapshot))

    def close(self):
        self.closed = True

    def emit_state(self, position, playing):
        self.on_state(type("PlaybackState", (), {
            "position": position,
            "playing": playing,
            "frame_count": self.session.frame_count,
        })())

    def emit_error(self, code="playback.output_write", cause="Output failed", recovery="Retry"):
        self.on_error(type("PlaybackError", (), {
            "code": code,
            "cause": cause,
            "recovery": recovery,
        })())


class MixerControllerTests(unittest.TestCase):
    def setUp(self):
        self.queue = QueuedExecution()
        self.states = []
        self.sessions = {}
        self.engines = []

        def load_session(folder):
            folder = Path(folder)
            if folder.name == "invalid":
                raise ValueError("missing bass.wav")
            session = FakeSession(folder)
            self.sessions[folder] = session
            return session

        def playback_factory(session, *, on_state, on_error):
            engine = FakePlayback(session, on_state=on_state, on_error=on_error)
            self.engines.append(engine)
            return engine

        self.controller = MixerController(
            load_session=load_session,
            playback_factory=playback_factory,
            start_worker=self.queue.start_worker,
            dispatch=self.queue.dispatch,
            on_change=self.states.append,
        )

    def finish_next_load(self):
        self.queue.workers.pop(0)()
        self.queue.events.pop(0)()

    def test_load_is_async_and_stale_result_is_rejected(self):
        self.assertTrue(self.controller.load("first"))
        self.assertTrue(self.controller.load("second"))
        self.assertEqual("loading", self.controller.state.phase)

        self.queue.workers.pop(0)()
        self.assertEqual([], self.queue.events)
        stale = self.engines[0]
        self.assertTrue(stale.closed)

        self.finish_next_load()
        self.assertEqual(Path("second"), self.controller.state.folder)
        self.assertIs(self.sessions[Path("second")], self.controller.state.session)
        self.assertEqual("ready", self.controller.state.phase)

    def test_invalid_session_blocks_transport_and_reports_actionable_error(self):
        self.assertFalse(self.controller.play())
        self.assertFalse(self.controller.pause())
        self.assertFalse(self.controller.seek(20))
        self.assertFalse(self.controller.set_volume("vocals.wav", 50))

        self.assertTrue(self.controller.load("invalid"))
        self.finish_next_load()
        self.assertEqual("error", self.controller.state.phase)
        self.assertIn("missing bass.wav", self.controller.state.detail)
        self.assertFalse(self.controller.play())

    def test_commands_and_immutable_settings_are_forwarded_after_load(self):
        self.assertTrue(self.controller.load("song"))
        self.finish_next_load()
        engine = self.engines[0]
        initial = self.controller.state.settings

        self.assertTrue(self.controller.play())
        self.assertTrue(self.controller.pause())
        self.assertTrue(self.controller.seek(25))
        self.assertTrue(self.controller.set_volume("vocals.wav", 50))
        self.assertTrue(self.controller.set_volume("bass", 25))
        self.assertTrue(self.controller.set_muted("drums.wav", True))
        self.assertTrue(self.controller.set_solo("vocals.wav", True))

        self.assertIsInstance(initial, MixerSnapshot)
        self.assertIsNot(initial, self.controller.state.settings)
        self.assertEqual(
            [("play",), ("pause",), ("seek", 25)],
            engine.commands[:3],
        )
        applied = [command for command in engine.commands if command[0] == "apply"]
        self.assertEqual(4, len(applied))
        settings = self.controller.state.settings.settings
        vocals = next(setting for setting in settings if setting.name == "vocals.wav")
        drums = next(setting for setting in settings if setting.name == "drums.wav")
        self.assertEqual(0.5, vocals.gain)
        self.assertTrue(vocals.solo)
        self.assertTrue(drums.muted)

    def test_playback_callbacks_are_dispatched_and_replacement_closes_old_engine(self):
        self.assertTrue(self.controller.load("first"))
        self.finish_next_load()
        old_engine = self.engines[0]
        old_engine.emit_state(12, True)
        self.assertEqual(1, len(self.queue.events))
        self.queue.events.pop()()
        self.assertEqual(12, self.controller.state.position)
        self.assertTrue(self.controller.state.playing)

        self.assertTrue(self.controller.load("second"))
        self.queue.workers.pop(0)()
        self.assertTrue(old_engine.closed)
        self.queue.events.pop(0)()
        self.assertEqual(Path("second"), self.controller.state.folder)

    def test_output_error_stops_state_with_one_actionable_message(self):
        self.assertTrue(self.controller.load("song"))
        self.finish_next_load()
        engine = self.engines[0]
        engine.emit_error(cause="device disconnected", recovery="Reconnect it")
        self.queue.events.pop()()

        self.assertEqual("error", self.controller.state.phase)
        self.assertIn("device disconnected", self.controller.state.detail)
        self.assertIn("Reconnect it", self.controller.state.detail)


class LoadTitleTests(unittest.TestCase):
    def test_load_and_manual_load_call_shapes_both_surface_title(self):
        queue = QueuedExecution()

        def load_session(folder):
            return FakeSession(Path(folder))

        controller = MixerController(
            load_session=load_session,
            playback_factory=lambda session, *, on_state, on_error: FakePlayback(
                session, on_state=on_state, on_error=on_error
            ),
            start_worker=queue.start_worker,
            dispatch=queue.dispatch,
        )

        # Auto-open call shape (from a completed separation).
        controller.load("first", title="song-one")
        queue.workers.pop(0)()
        queue.events.pop(0)()
        self.assertEqual("song-one", controller.state.title)

        # Manual "Load stems folder" call shape.
        controller.load("second", title="song-two")
        queue.workers.pop(0)()
        queue.events.pop(0)()
        self.assertEqual("song-two", controller.state.title)

    def test_load_without_title_defaults_to_empty_string(self):
        queue = QueuedExecution()
        controller = MixerController(
            load_session=lambda folder: FakeSession(Path(folder)),
            playback_factory=lambda session, *, on_state, on_error: FakePlayback(
                session, on_state=on_state, on_error=on_error
            ),
            start_worker=queue.start_worker,
            dispatch=queue.dispatch,
        )

        controller.load("first")
        queue.workers.pop(0)()
        queue.events.pop(0)()

        self.assertEqual("", controller.state.title)


class ExportTargetTests(unittest.TestCase):
    def test_names_target_from_title_and_stem(self):
        with tempfile.TemporaryDirectory() as destination:
            destination = Path(destination)

            target = _export_target(destination, "mysong", "vocals.wav")

            self.assertEqual(destination / "mysong-vocals.wav", target)

    def test_falls_back_to_stem_name_without_a_title(self):
        with tempfile.TemporaryDirectory() as destination:
            destination = Path(destination)

            target = _export_target(destination, "", "vocals.wav")

            self.assertEqual(destination / "vocals.wav", target)

    def test_resolves_collisions_with_bounded_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as destination:
            destination = Path(destination)
            (destination / "mysong-vocals.wav").write_bytes(b"x")

            second = _export_target(destination, "mysong", "vocals.wav")
            self.assertEqual(destination / "mysong-vocals (2).wav", second)

            second.write_bytes(b"y")
            third = _export_target(destination, "mysong", "vocals.wav")
            self.assertEqual(destination / "mysong-vocals (3).wav", third)


class ExportStemTests(unittest.TestCase):
    def setUp(self):
        self.queue = QueuedExecution()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source_folder = Path(self.tmp.name) / "stems"
        self.source_folder.mkdir()
        for name in STEMS:
            (self.source_folder / name).write_bytes(f"content:{name}".encode("ascii"))
        self.destination = Path(self.tmp.name) / "destination"
        self.destination.mkdir()

        self.controller = MixerController(
            load_session=lambda folder: FakeSession(Path(folder)),
            playback_factory=lambda session, *, on_state, on_error: FakePlayback(
                session, on_state=on_state, on_error=on_error
            ),
            start_worker=self.queue.start_worker,
            dispatch=self.queue.dispatch,
        )

    def load(self, *, title=""):
        self.controller.load(self.source_folder, title=title)
        self.queue.workers.pop(0)()
        self.queue.events.pop(0)()

    def test_returns_none_without_a_session(self):
        self.assertIsNone(self.controller.export_stem("vocals.wav", self.destination))

    def test_returns_none_after_close(self):
        self.load(title="mysong")
        self.controller.close()

        self.assertIsNone(self.controller.export_stem("vocals.wav", self.destination))

    def test_returns_none_for_unknown_stem(self):
        self.load(title="mysong")

        self.assertIsNone(self.controller.export_stem("piano.wav", self.destination))

    def test_returns_none_for_non_directory_destination_without_setting_error_code(self):
        self.load(title="mysong")
        not_a_directory = Path(self.tmp.name) / "not-a-directory.txt"
        not_a_directory.write_bytes(b"x")

        result = self.controller.export_stem("vocals.wav", not_a_directory)

        self.assertIsNone(result)
        self.assertIsNone(self.controller.state.error_code)

    def test_copies_raw_bytes_and_returns_the_written_path(self):
        self.load(title="mysong")

        written = self.controller.export_stem("vocals.wav", self.destination)

        self.assertEqual(self.destination / "mysong-vocals.wav", written)
        self.assertEqual(b"content:vocals.wav", written.read_bytes())
        self.assertTrue((self.source_folder / "vocals.wav").exists())

    def test_sets_error_code_on_post_gate_os_error(self):
        self.load(title="mysong")
        target = self.destination / "mysong-vocals.wav"
        target.write_bytes(b"already-there")

        with mock.patch("SeparationWorker.mixer_controller._export_target", return_value=target):
            result = self.controller.export_stem("vocals.wav", self.destination)

        self.assertIsNone(result)
        self.assertEqual("export.failed", self.controller.state.error_code)


class ExportStemsBatchTests(unittest.TestCase):
    def setUp(self):
        self.queue = QueuedExecution()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source_folder = Path(self.tmp.name) / "stems"
        self.source_folder.mkdir()
        for name in STEMS:
            (self.source_folder / name).write_bytes(f"content:{name}".encode("ascii"))
        self.destination = Path(self.tmp.name) / "destination"
        self.destination.mkdir()

        self.controller = MixerController(
            load_session=lambda folder: FakeSession(Path(folder)),
            playback_factory=lambda session, *, on_state, on_error: FakePlayback(
                session, on_state=on_state, on_error=on_error
            ),
            start_worker=self.queue.start_worker,
            dispatch=self.queue.dispatch,
        )

    def load(self, *, title=""):
        self.controller.load(self.source_folder, title=title)
        self.queue.workers.pop(0)()
        self.queue.events.pop(0)()

    def assert_gate_refusal(self):
        self.assertEqual("idle", self.controller.state.export_phase)
        self.assertEqual([], self.queue.workers)

    def test_returns_false_without_a_session(self):
        result = self.controller.export_stems(["vocals.wav", "drums.wav"], self.destination)

        self.assertIs(False, result)
        self.assert_gate_refusal()

    def test_returns_false_after_close(self):
        self.load(title="mysong")
        self.controller.close()

        result = self.controller.export_stems(["vocals.wav"], self.destination)

        self.assertIs(False, result)
        self.assert_gate_refusal()

    def test_returns_false_for_an_unknown_stem_mixed_into_the_batch(self):
        self.load(title="mysong")

        result = self.controller.export_stems(["vocals.wav", "piano.wav"], self.destination)

        self.assertIs(False, result)
        self.assert_gate_refusal()

    def test_returns_false_for_non_directory_destination(self):
        self.load(title="mysong")
        not_a_directory = Path(self.tmp.name) / "not-a-directory.txt"
        not_a_directory.write_bytes(b"x")

        result = self.controller.export_stems(["vocals.wav"], not_a_directory)

        self.assertIs(False, result)
        self.assert_gate_refusal()

    def test_returns_false_for_an_empty_stem_list(self):
        self.load(title="mysong")

        result = self.controller.export_stems([], self.destination)

        self.assertIs(False, result)
        self.assert_gate_refusal()

    def test_batch_exports_every_requested_stem_in_order(self):
        self.load(title="mysong")

        result = self.controller.export_stems(["vocals.wav", "drums.wav"], self.destination)

        self.assertIs(True, result)
        self.assertEqual("running", self.controller.state.export_phase)

        self.queue.workers.pop(0)()
        self.queue.events.pop(0)()

        self.assertEqual("done", self.controller.state.export_phase)
        self.assertEqual(
            (
                ("vocals.wav", self.destination / "mysong-vocals.wav", None),
                ("drums.wav", self.destination / "mysong-drums.wav", None),
            ),
            self.controller.state.export_results,
        )
        self.assertEqual(b"content:vocals.wav", (self.destination / "mysong-vocals.wav").read_bytes())
        self.assertEqual(b"content:drums.wav", (self.destination / "mysong-drums.wav").read_bytes())

    def test_one_failing_stem_does_not_block_the_rest_of_the_batch(self):
        self.load(title="mysong")
        colliding_target = self.destination / "mysong-drums.wav"
        colliding_target.write_bytes(b"already-there")

        import SeparationWorker.mixer_controller as mixer_controller_module

        real_export_target = mixer_controller_module._export_target

        def fake_export_target(destination, title, stem_name):
            if stem_name == "drums.wav":
                return colliding_target
            return real_export_target(destination, title, stem_name)

        with mock.patch("SeparationWorker.mixer_controller._export_target", side_effect=fake_export_target):
            self.assertIs(True, self.controller.export_stems(["vocals.wav", "drums.wav"], self.destination))
            self.queue.workers.pop(0)()
            self.queue.events.pop(0)()

        self.assertEqual("done", self.controller.state.export_phase)
        results = dict((name, (path, code)) for name, path, code in self.controller.state.export_results)
        self.assertEqual(
            (self.destination / "mysong-vocals.wav", None),
            results["vocals.wav"],
        )
        self.assertEqual((None, "export.failed"), results["drums.wav"])
        self.assertEqual(b"content:vocals.wav", (self.destination / "mysong-vocals.wav").read_bytes())


METAL_LANES = (
    "vocals.wav",
    "drums.wav",
    "bass.wav",
    "lead_guitar.wav",
    "rhythm_guitar.wav",
    "other.wav",
)


@dataclass(frozen=True)
class FakeProfileSession:
    """A published result that carries its own lane layout."""

    folder: Path
    names: tuple = METAL_LANES
    absent: tuple = ("lead_guitar",)
    frame_count: int = 100
    sample_rate: int = 10
    channels: int = 2

    @property
    def paths(self):
        return tuple(self.folder / name for name in self.names)


class ProfileLaneMixerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        for name in METAL_LANES:
            (self.folder / name).write_bytes(b"RIFF-fake-" + name.encode("ascii"))
        self.controller = MixerController(
            session_loader=lambda folder: FakeProfileSession(Path(folder)),
            playback_factory=FakePlayback,
            start_worker=lambda target: target(),
            dispatch=lambda callback: callback(),
        )
        self.controller.load(self.folder, title="song")

    def tearDown(self):
        self.controller.close()
        self.temporary.cleanup()

    def test_session_exposes_every_published_lane_in_order(self):
        state = self.controller.state

        self.assertEqual("ready", state.phase)
        self.assertEqual("6 stems are ready", state.headline)
        self.assertEqual(METAL_LANES, state.lane_names)
        self.assertEqual(
            METAL_LANES, tuple(setting.name for setting in state.settings.settings)
        )

    def test_absent_lane_is_reported_rather_than_hidden(self):
        state = self.controller.state

        self.assertEqual(("lead_guitar",), state.absent_lanes)
        self.assertTrue(state.is_absent("lead_guitar.wav"))
        self.assertFalse(state.is_absent("rhythm_guitar.wav"))
        self.assertIn("lead_guitar.wav", state.lane_names)

    def test_role_lanes_accept_mute_solo_and_gain(self):
        self.assertTrue(self.controller.set_muted("lead_guitar.wav", True))
        self.assertTrue(self.controller.set_solo("rhythm_guitar", True))
        self.assertTrue(self.controller.set_volume("rhythm_guitar.wav", 50))

        settings = {item.name: item for item in self.controller.state.settings.settings}
        self.assertTrue(settings["lead_guitar.wav"].muted)
        self.assertTrue(settings["rhythm_guitar.wav"].solo)
        self.assertLess(settings["rhythm_guitar.wav"].gain, 1.0)

    def test_an_absent_lane_is_still_controllable(self):
        self.assertTrue(self.controller.toggle_mute("lead_guitar.wav"))

        settings = {item.name: item for item in self.controller.state.settings.settings}
        self.assertTrue(settings["lead_guitar.wav"].muted)

    def test_a_lane_outside_the_published_layout_is_refused(self):
        self.assertFalse(self.controller.set_muted("guitar.wav", True))
        self.assertFalse(self.controller.set_volume("piano.wav", 50))

    def test_role_lanes_export_as_published_bytes(self):
        with tempfile.TemporaryDirectory() as destination:
            written = self.controller.export_stem("lead_guitar.wav", destination)

            self.assertIsNotNone(written)
            self.assertEqual("song-lead_guitar.wav", written.name)
            self.assertEqual((self.folder / "lead_guitar.wav").read_bytes(), written.read_bytes())

    def test_batch_export_covers_every_published_lane(self):
        with tempfile.TemporaryDirectory() as destination:
            self.assertTrue(self.controller.export_stems(METAL_LANES, destination))

            results = self.controller.state.export_results
            self.assertEqual("done", self.controller.state.export_phase)
            self.assertEqual(len(METAL_LANES), len(results))
            self.assertTrue(all(code is None and path is not None for _name, path, code in results))

    def test_a_failed_load_falls_back_to_the_legacy_layout(self):
        def failing_loader(_folder):
            raise ValueError("broken folder")

        controller = MixerController(
            session_loader=failing_loader,
            playback_factory=FakePlayback,
            start_worker=lambda target: target(),
            dispatch=lambda callback: callback(),
        )
        controller.load(self.folder, title="song")

        self.assertEqual("error", controller.state.phase)
        self.assertEqual(STEMS, controller.state.lane_names)
        self.assertEqual((), controller.state.absent_lanes)


if __name__ == "__main__":
    unittest.main()
