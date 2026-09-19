import unittest
from pathlib import Path

from SeparationWorker.engine.mixer import MixSetting, MixerSnapshot
from SeparationWorker.gui import (
    APP_NAME,
    MixerViewModel,
    library_row_detail,
    main,
    parse_drop_paths,
    space_toggles_playback,
)
from SeparationWorker.history import TrackRecord
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


METAL_LANES = (
    "vocals.wav",
    "drums.wav",
    "bass.wav",
    "lead_guitar.wav",
    "rhythm_guitar.wav",
    "other.wav",
)


class ProfileLaneViewModelTests(unittest.TestCase):
    def view(self, *, absent=(), lane_names=METAL_LANES):
        state = MixerState(
            folder=Path("stems"),
            session=FakeSession(),
            phase="ready",
            frame_count=100,
            settings=MixerSnapshot(tuple(MixSetting(name) for name in lane_names)),
            lane_names=lane_names,
            absent_lanes=absent,
        )
        return MixerViewModel.from_controller_state(state)

    def test_every_published_lane_is_rendered_in_profile_order(self):
        view = self.view()

        self.assertEqual(METAL_LANES, view.lane_names)
        self.assertEqual(METAL_LANES, tuple(row[0] for row in view.lane_rows))
        self.assertEqual("LEAD GUITAR", view.lane_rows[3][1])
        self.assertEqual("RHYTHM GUITAR", view.lane_rows[4][1])

    def test_an_absent_lane_keeps_its_row_and_is_labeled(self):
        view = self.view(absent=("lead_guitar",))

        self.assertTrue(view.is_absent("lead_guitar.wav"))
        self.assertFalse(view.is_absent("rhythm_guitar.wav"))
        name, label, _color, absent = view.lane_rows[3]
        self.assertEqual("lead_guitar.wav", name)
        self.assertTrue(absent)
        self.assertIn("NOT IN THIS TRACK", label)
        self.assertEqual(6, len(view.lane_rows))

    def test_role_lanes_have_their_own_colours(self):
        view = self.view()
        colors = {row[0]: row[2] for row in view.lane_rows}

        self.assertNotEqual(colors["lead_guitar.wav"], colors["rhythm_guitar.wav"])
        self.assertNotEqual(colors["lead_guitar.wav"], colors["other.wav"])

    def test_an_unknown_lane_still_renders_readably(self):
        view = self.view(lane_names=("vocals.wav", "hammond_organ.wav"))

        name, label, color, absent = view.lane_rows[1]
        self.assertEqual("hammond_organ.wav", name)
        self.assertEqual("HAMMOND ORGAN", label)
        self.assertTrue(color.startswith("#"))
        self.assertFalse(absent)

    def test_the_legacy_layout_is_unchanged(self):
        view = self.view(lane_names=("vocals.wav", "drums.wav", "bass.wav", "other.wav"))

        self.assertEqual(
            ("VOCALS", "DRUMS", "BASS", "OTHER"), tuple(row[1] for row in view.lane_rows)
        )
        self.assertEqual((), view.absent_names)


class GuiEntrypointTests(unittest.TestCase):
    def test_self_test_does_not_create_a_window(self):
        self.assertEqual(0, main(["--self-test"]))


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


class LibraryRowDetailTests(unittest.TestCase):
    """The detail column shows only what the catalog actually knows.

    BPM and musical key are never computed, so a column that printed them
    read as `BPM —  ·  Key —` on every row. Duration and the local date
    are the two values every record carries.
    """

    def make_record(self, **changes):
        fields = dict(
            track_id="t1",
            source_path="C:/music/song.mp3",
            source_hash=None,
            title="Song",
            artist="Artist",
            genre="Metal",
            duration_seconds=363.4,
            bpm=None,
            musical_key=None,
            created_at_utc="2026-09-16T12:00:00Z",
            profile_id="legacy",
            pipeline_fingerprint="fp",
            result_directory=Path("results/t1"),
            status="ready",
            error_detail=None,
        )
        fields.update(changes)
        return TrackRecord(**fields)

    def test_shows_duration_and_local_date_only(self):
        detail = library_row_detail(self.make_record())

        self.assertEqual("06:03  ·  2026-09-16", detail)
        self.assertNotIn("BPM", detail)
        self.assertNotIn("Key", detail)
        self.assertNotIn("Metal", detail)

    def test_prefixes_the_profile_name_only_when_asked(self):
        record = self.make_record()

        self.assertEqual("Legacy  ·  06:03  ·  2026-09-16", library_row_detail(record, profile_name="Legacy"))

    def test_missing_duration_or_unparseable_date_render_as_dashes(self):
        record = self.make_record(duration_seconds=None, created_at_utc="not-a-date")

        self.assertEqual("—  ·  —", library_row_detail(record))


class BrandingTests(unittest.TestCase):
    def test_uses_the_approved_product_name(self):
        self.assertEqual("Stemslayer", APP_NAME)


class SpacebarTransportGuardTests(unittest.TestCase):
    def test_allows_when_view_is_mixer_and_can_play_is_true(self):
        self.assertTrue(space_toggles_playback("mixer", True))

    def test_denies_when_can_play_is_false(self):
        self.assertFalse(space_toggles_playback("mixer", False))

    def test_denies_when_view_is_not_mixer(self):
        self.assertFalse(space_toggles_playback("separation", True))

    def test_denies_when_view_is_not_mixer_and_cannot_play(self):
        self.assertFalse(space_toggles_playback("separation", False))


if __name__ == "__main__":
    unittest.main()
