import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from SeparationWorker.engine.mixer import MixSetting, MixerSnapshot
from SeparationWorker.engine.playback import PlaybackEngine


STEMS = ("vocals.wav", "drums.wav", "bass.wav", "other.wav")


@dataclass(frozen=True)
class FakeSession:
    frame_count: int = 8
    channels: int = 1
    sample_rate: int = 8_000

    @property
    def paths(self):
        return tuple(Path(name) for name in STEMS)


class FakeReader:
    def __init__(self, values, *, channels=1):
        self.values = np.asarray(values, dtype=np.float32).reshape(-1, channels)
        self.cursor = 0
        self.closed = False
        self.reads = []

    def read(self, frames, dtype="float32", always_2d=True):
        del dtype, always_2d
        self.reads.append((self.cursor, frames))
        block = self.values[self.cursor : self.cursor + frames]
        self.cursor += len(block)
        return block.copy()

    def seek(self, frame):
        self.cursor = frame

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, *, fail_write=False, fail_write_after=None, write_delay=0.0, **kwargs):
        self.kwargs = kwargs
        self.fail_write = fail_write
        self.fail_write_after = fail_write_after
        self.write_delay = write_delay
        self.started = False
        self.stopped = False
        self.closed = False
        self.writes = []

    def start(self):
        self.started = True

    def write(self, block):
        if self.write_delay:
            time.sleep(self.write_delay)
        if self.fail_write:
            raise RuntimeError("fake output disconnected")
        self.writes.append(np.asarray(block).copy())
        if self.fail_write_after is not None and len(self.writes) >= self.fail_write_after:
            raise RuntimeError("fake output disconnected after one block")

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.readers = []
        self.streams = []
        self.states = []
        self.errors = []
        self.state_event = threading.Event()
        self.frame_count = 8
        self.stream_delay = 0.0

    def reader_factory(self, path):
        index = STEMS.index(Path(path).name)
        reader = FakeReader((index + 1.0,) * self.frame_count)
        self.readers.append(reader)
        return reader

    def stream_factory(self, **kwargs):
        stream = FakeStream(**kwargs, write_delay=self.stream_delay)
        self.streams.append(stream)
        return stream

    def on_state(self, state):
        self.states.append(state)
        self.state_event.set()

    def on_error(self, error):
        self.errors.append(error)
        self.state_event.set()

    def make_engine(self, session=None, *, stream_factory=None):
        session = session or FakeSession()
        self.frame_count = session.frame_count
        return PlaybackEngine(
            session,
            stream_factory=stream_factory or self.stream_factory,
            reader_factory=self.reader_factory,
            on_state=self.on_state,
            on_error=self.on_error,
            block_size=2,
        )

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.state_event.wait(0.01)
            self.state_event.clear()
        self.fail("timed out waiting for playback state")

    def test_mix_uses_shared_cursor_and_clips_summed_float32_output(self):
        def reader_factory(path):
            index = STEMS.index(Path(path).name)
            values = ((0.75,) * 8) if index < 2 else ((0.0,) * 8)
            reader = FakeReader(values)
            self.readers.append(reader)
            return reader

        engine = PlaybackEngine(
            FakeSession(),
            stream_factory=self.stream_factory,
            reader_factory=reader_factory,
            on_state=self.on_state,
            on_error=self.on_error,
            block_size=2,
        )
        try:
            engine.play()
            self.wait_until(lambda: not engine.playing and engine.position == 8)
            self.assertEqual(4, len(self.readers))
            self.assertTrue(all(reader.reads[0][0] == 0 for reader in self.readers))
            self.assertTrue(all(reader.closed for reader in self.readers))
            self.assertTrue(all(np.allclose(block, 1.0) for block in self.streams[0].writes))
            self.assertEqual(8, engine.position)
        finally:
            engine.close()

    def test_pause_resume_preserves_position_and_seek_clamps(self):
        self.stream_delay = 0.001
        engine = self.make_engine(FakeSession(frame_count=800))
        try:
            engine.play()
            self.wait_until(lambda: engine.playing and engine.position >= 2)
            engine.pause()
            self.wait_until(lambda: not engine.playing)
            paused = engine.position
            engine.seek(-10)
            self.wait_until(lambda: engine.position == 0)
            engine.seek(1000)
            self.wait_until(lambda: engine.position == 800)
            engine.seek(3)
            self.wait_until(lambda: engine.position == 3)
            engine.play()
            self.wait_until(lambda: not engine.playing and engine.position == 800)
            self.assertGreaterEqual(paused, 2)
        finally:
            engine.close()

    def test_play_at_end_restarts_from_zero(self):
        engine = self.make_engine()
        try:
            engine.play()
            self.wait_until(lambda: not engine.playing and engine.position == 8)
            engine.play()
            self.wait_until(lambda: len(self.streams) >= 2 and not engine.playing)
            self.assertEqual(0, self.readers[4].reads[0][0])
        finally:
            engine.close()

    def test_live_snapshot_applies_volume_mute_and_solo(self):
        self.stream_delay = 0.001
        engine = self.make_engine(FakeSession(frame_count=800))
        try:
            engine.play()
            self.wait_until(lambda: len(self.streams) >= 1 and len(self.streams[0].writes) >= 1)
            engine.pause()
            self.wait_until(lambda: not engine.playing)
            initial_writes = len(self.streams[0].writes)
            engine.apply(
                MixerSnapshot(
                    (
                        MixSetting("vocals.wav", gain=0.5, solo=True),
                        MixSetting("drums.wav", gain=1.0),
                        MixSetting("bass.wav", gain=1.0, muted=True),
                        MixSetting("other.wav", gain=1.0),
                    )
                )
            )
            engine.seek(0)
            engine.play()
            self.wait_until(lambda: len(self.streams[0].writes) > initial_writes)
            self.assertTrue(np.allclose(self.streams[0].writes[-1], 0.5))
        finally:
            engine.close()

    def test_open_failure_reports_once_and_cleans_readers(self):
        def failing_stream(**kwargs):
            del kwargs
            raise RuntimeError("no output device")

        engine = self.make_engine(stream_factory=failing_stream)
        engine.play()
        self.wait_until(lambda: self.errors)
        try:
            self.assertEqual(1, len(self.errors))
            self.assertIn("output", self.errors[0].code)
            self.assertTrue(all(reader.closed for reader in self.readers))
            self.assertFalse(engine.playing)
        finally:
            engine.close()

    def test_mid_write_failure_stops_writes_and_cleans_everything(self):
        def failing_stream(**kwargs):
            stream = FakeStream(**kwargs, fail_write_after=1)
            self.streams.append(stream)
            return stream

        engine = self.make_engine(stream_factory=failing_stream)
        engine.play()
        self.wait_until(lambda: self.errors)
        try:
            self.assertEqual(1, len(self.errors))
            self.assertIn("output", self.errors[0].code)
            self.assertFalse(engine.playing)
            self.assertTrue(self.streams[0].stopped)
            self.assertTrue(self.streams[0].closed)
            self.assertTrue(all(reader.closed for reader in self.readers))
            writes = len(self.streams[0].writes)
            time.sleep(0.05)
            self.assertEqual(writes, len(self.streams[0].writes))
        finally:
            engine.close()

    def test_replace_during_playback_closes_old_resources_before_new_session(self):
        self.stream_delay = 0.001
        first = FakeSession(frame_count=800)
        second = FakeSession(frame_count=4)
        engine = self.make_engine(first)
        try:
            engine.play()
            self.wait_until(lambda: len(self.streams) == 1 and engine.position >= 2)
            old_readers = tuple(self.readers)
            old_stream = self.streams[0]
            engine.replace(second)
            self.wait_until(lambda: engine.position == 0 and not engine.playing)
            self.assertTrue(old_stream.closed)
            self.assertTrue(all(reader.closed for reader in old_readers))
            engine.play()
            self.wait_until(lambda: len(self.streams) == 2 and not engine.playing)
            self.assertEqual(4, engine.position)
        finally:
            engine.close()

    def test_close_during_playback_is_safe_and_idempotent(self):
        engine = self.make_engine()
        engine.play()
        self.wait_until(lambda: self.streams and self.streams[0].started)
        engine.close()
        engine.close()
        self.assertFalse(engine.playing)
        self.assertTrue(self.streams[0].closed)
        self.assertTrue(all(reader.closed for reader in self.readers))
        writes = len(self.streams[0].writes)
        time.sleep(0.05)
        self.assertEqual(writes, len(self.streams[0].writes))


if __name__ == "__main__":
    unittest.main()
