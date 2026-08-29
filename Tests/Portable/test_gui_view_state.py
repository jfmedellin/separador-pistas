import unittest
from pathlib import Path

from SeparationWorker.engine.mixer import MixSetting, MixerSnapshot
from SeparationWorker.gui import APP_NAME, MixerViewModel, parse_drop_paths
from SeparationWorker.mixer_controller import MixerState


class FakeSession:
    sample_rate = 10
    frame_count = 100


class MixerViewModelTests(unittest.TestCase):
    def make_state(self, **changes):
        state = MixerState(
            folder=Path("stems"),
            session=FakeSession(),
            phase="playing",
            position=25,
            frame_count=100,
            playing=True,
            settings=MixerSnapshot(
                (
                    MixSetting("vocals.wav", gain=0.5, muted=True),
                    MixSetting("drums.wav", solo=True),
                    MixSetting("bass.wav"),
                    MixSetting("other.wav"),
                )
            ),
        )
        return MixerViewModel.from_controller_state(changes.pop("state", state), **changes)

    def test_exposes_exact_four_lanes_and_shared_timeline(self):
        view = self.make_state()

        self.assertEqual(("vocals.wav", "drums.wav", "bass.wav", "other.wav"), view.lane_names)
        self.assertEqual(25, view.position)
        self.assertEqual(100, view.frame_count)
        self.assertEqual(0.25, view.playhead_ratio)
        self.assertEqual(10.0, view.duration_seconds)
        self.assertEqual(2.5, view.position_seconds)

    def test_seek_maps_pixels_to_one_clamped_shared_frame(self):
        view = self.make_state()

        self.assertEqual(0, view.frame_for_pixel(-10, 400))
        self.assertEqual(50, view.frame_for_pixel(200, 400))
        self.assertEqual(100, view.frame_for_pixel(999, 400))

    def test_controls_are_readable_and_excluded_features_have_no_view_state(self):
        view = self.make_state()

        self.assertEqual({"vocals.wav": 50.0, "drums.wav": 100.0, "bass.wav": 100.0, "other.wav": 100.0}, view.volume_percent)
        self.assertEqual(("vocals.wav",), view.muted_names)
        self.assertEqual(("drums.wav",), view.soloed_names)
        self.assertEqual((), view.excluded_controls)

    def test_preview_preserves_transport_and_clamps_position(self):
        view = self.make_state()
        preview = view.with_preview(76)

        self.assertEqual(76, preview.preview_frame)
        self.assertEqual(25, preview.position)
        self.assertEqual(100, preview.frame_count)
        self.assertEqual(0.76, preview.preview_ratio)


class DropPathParsingTests(unittest.TestCase):
    def test_uses_tcl_splitter_to_preserve_paths_with_spaces(self):
        payload = r"{C:\Music Library\first song.wav} C:\second.wav"
        received = []

        def splitlist(data):
            received.append(data)
            return (r"C:\Music Library\first song.wav", r"C:\second.wav")

        self.assertEqual(
            (r"C:\Music Library\first song.wav", r"C:\second.wav"),
            parse_drop_paths(payload, splitlist),
        )
        self.assertEqual([payload], received)

    def test_empty_or_malformed_payload_is_ignored(self):
        def malformed(_data):
            raise TypeError("malformed Tcl list")

        self.assertEqual((), parse_drop_paths("", malformed))
        self.assertEqual((), parse_drop_paths("{unterminated", malformed))


class BrandingTests(unittest.TestCase):
    def test_uses_the_approved_product_name(self):
        self.assertEqual("Stemslayer", APP_NAME)


if __name__ == "__main__":
    unittest.main()
