import unittest
from dataclasses import dataclass
from pathlib import Path

from SeparationWorker.engine.mixer import MixerSnapshot
from SeparationWorker.mixer_controller import MixerController


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


if __name__ == "__main__":
    unittest.main()
