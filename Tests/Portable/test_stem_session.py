import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from SeparationWorker.engine.stem_session import (
    PEAK_BIN_COUNT,
    STEM_NAMES,
    StemSession,
    StemSessionError,
)


def write_pcm_wav(path, frames, *, rate=8_000, channels=1):
    path = Path(path)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
                for frame in frames
                for sample in (frame if isinstance(frame, tuple) else (frame,))
            )
        )


class StemSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_set(self, *, rate=8_000, channels=1, frame_count=4_000):
        frames = (0.0,) * frame_count if channels == 1 else ((0.0, 0.0),) * frame_count
        for name in STEM_NAMES:
            write_pcm_wav(self.folder / name, frames, rate=rate, channels=channels)

    def test_loads_exact_stems_with_shared_metadata_and_peak_envelopes(self):
        frame_count = 4_000
        frames = [0.0] * frame_count
        frames[1_000] = 0.5
        for name in STEM_NAMES:
            write_pcm_wav(self.folder / name, frames)

        session = StemSession.load(self.folder)

        self.assertEqual(self.folder.resolve(), session.folder)
        self.assertEqual(tuple((self.folder / name).resolve() for name in STEM_NAMES), session.paths)
        self.assertEqual((8_000, 1, frame_count), (session.sample_rate, session.channels, session.frame_count))
        self.assertEqual(len(STEM_NAMES), len(session.peaks))
        self.assertTrue(all(len(envelope) == PEAK_BIN_COUNT for envelope in session.peaks))
        self.assertAlmostEqual(0.5, session.peaks[0][500], places=4)
        self.assertEqual(0.0, session.peaks[0][0])
        self.assertTrue(all(math.isfinite(value) and value >= 0 for envelope in session.peaks for value in envelope))

    def test_rejects_missing_or_unreadable_required_stems(self):
        self.write_set()
        (self.folder / "other.wav").unlink()
        with self.assertRaises(StemSessionError) as missing:
            StemSession.load(self.folder)
        self.assertEqual("stem.missing", missing.exception.code)
        self.assertIn("other.wav", missing.exception.cause)

        (self.folder / "other.wav").write_bytes(b"not a wav")
        with self.assertRaises(StemSessionError) as unreadable:
            StemSession.load(self.folder)
        self.assertEqual("stem.unreadable", unreadable.exception.code)
        self.assertIn("other.wav", unreadable.exception.cause)

    def test_rejects_misaligned_sample_rate_channels_and_frame_count(self):
        cases = (
            ("sample rate", {"rate": 16_000}, "stem.sample_rate_mismatch"),
            ("channels", {"channels": 2}, "stem.channel_count_mismatch"),
            ("frames", {"frame_count": 3_999}, "stem.frame_count_mismatch"),
        )
        for label, options, code in cases:
            with self.subTest(label=label):
                self.write_set()
                changed = self.folder / "other.wav"
                frame_count = options.get("frame_count", 4_000)
                channels = options.get("channels", 1)
                frames = (0.0,) * frame_count if channels == 1 else ((0.0, 0.0),) * frame_count
                write_pcm_wav(changed, frames, rate=options.get("rate", 8_000), channels=channels)

                with self.assertRaises(StemSessionError) as caught:
                    StemSession.load(self.folder)

                self.assertEqual(code, caught.exception.code)
                self.assertIn("other.wav", caught.exception.cause)

    def test_rejects_a_folder_that_does_not_exist(self):
        with self.assertRaises(StemSessionError) as caught:
            StemSession.load(self.folder / "missing")

        self.assertEqual("stem.folder_missing", caught.exception.code)
        self.assertIn("folder", caught.exception.recovery.lower())


if __name__ == "__main__":
    unittest.main()
