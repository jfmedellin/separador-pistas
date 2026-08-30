import unittest

import numpy as np

from SeparationWorker.engine.role_metrics import peak_dbfs, signal_to_residual_db
from SeparationWorker.engine.stereo_split import (
    SPLITTER_ID,
    StereoSplitError,
    split_center_sides,
)

SAMPLE_RATE = 44100


def tone(frequency, amplitude=0.4, seconds=0.25):
    frames = int(SAMPLE_RATE * seconds)
    time = np.arange(frames, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)


def stereo(left, right):
    return np.stack([left, right], axis=1).astype(np.float32)


class ReconstructionTests(unittest.TestCase):
    def test_the_pair_reproduces_the_input_far_above_any_gate(self):
        source = stereo(tone(880.0), tone(110.0, amplitude=0.5))

        center, sides = split_center_sides(source)

        measured = signal_to_residual_db(source, center + sides)
        self.assertGreater(measured, 120.0)

    def test_reconstruction_holds_for_a_hard_panned_pair(self):
        rhythm = tone(110.0, amplitude=0.5)
        source = stereo(rhythm, -rhythm)

        center, sides = split_center_sides(source)

        self.assertGreater(signal_to_residual_db(source, center + sides), 120.0)

    def test_the_split_is_deterministic(self):
        source = stereo(tone(880.0), tone(110.0, amplitude=0.5))

        first_center, first_sides = split_center_sides(source)
        second_center, second_sides = split_center_sides(source)

        np.testing.assert_array_equal(first_center, second_center)
        np.testing.assert_array_equal(first_sides, second_sides)


class PositionTests(unittest.TestCase):
    def test_a_centred_part_lands_in_the_centre_lane(self):
        solo = tone(880.0)
        source = stereo(solo, solo)

        center, sides = split_center_sides(source)

        np.testing.assert_allclose(source, center, atol=1e-6)
        self.assertEqual(-np.inf, peak_dbfs(sides))

    def test_hard_panned_doubles_land_in_the_sides_lane(self):
        rhythm = tone(110.0, amplitude=0.5)
        source = stereo(rhythm, -rhythm)

        center, sides = split_center_sides(source)

        self.assertEqual(-np.inf, peak_dbfs(center))
        np.testing.assert_allclose(source, sides, atol=1e-6)

    def test_a_centred_solo_over_panned_rhythm_separates(self):
        solo = tone(880.0, amplitude=0.4)
        rhythm = tone(110.0, amplitude=0.5)
        source = stereo(solo + rhythm, solo - rhythm)

        center, sides = split_center_sides(source)

        np.testing.assert_allclose(stereo(solo, solo), center, atol=1e-6)
        np.testing.assert_allclose(stereo(rhythm, -rhythm), sides, atol=1e-6)

    def test_position_is_not_role_a_centred_rhythm_lands_in_the_centre(self):
        """The honest limit of this technique, pinned as a test.

        A rhythm part mixed to the centre is indistinguishable from a solo
        here. This is why the lanes are named for position, never for role.
        """
        centred_rhythm = tone(110.0, amplitude=0.5)
        source = stereo(centred_rhythm, centred_rhythm)

        center, sides = split_center_sides(source)

        np.testing.assert_allclose(source, center, atol=1e-6)
        self.assertEqual(-np.inf, peak_dbfs(sides))


class ChannelLayoutTests(unittest.TestCase):
    def test_mono_input_is_all_centre_with_silent_sides(self):
        mono = tone(880.0).reshape(-1, 1)

        center, sides = split_center_sides(mono)

        np.testing.assert_array_equal(mono, center)
        self.assertEqual(-np.inf, peak_dbfs(sides))
        self.assertEqual(mono.shape, sides.shape)

    def test_more_than_two_channels_is_rejected(self):
        surround = np.zeros((100, 6), dtype=np.float32)

        with self.assertRaises(StereoSplitError):
            split_center_sides(surround)

    def test_a_non_planar_signal_is_rejected(self):
        with self.assertRaises(StereoSplitError):
            split_center_sides(np.zeros(100, dtype=np.float32))

    def test_an_empty_signal_is_rejected(self):
        with self.assertRaises(StereoSplitError):
            split_center_sides(np.zeros((0, 2), dtype=np.float32))


class IdentityTests(unittest.TestCase):
    def test_the_splitter_declares_a_versioned_identity(self):
        self.assertEqual("center-sides-v1", SPLITTER_ID)


if __name__ == "__main__":
    unittest.main()
