"""Real-widget coverage for the mixer lane strip.

These tests build an actual Tk window, so they prove the lane strip is
rebuilt from the published layout rather than trusting the headless view
model alone. They skip themselves when no display is available.
"""

import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path

from SeparationWorker.engine.mixer import MixSetting, MixerSnapshot
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE_ID, METAL_PROFILE_ID
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.mixer_controller import MixerState

try:
    import customtkinter as ctk
    from tkinterdnd2 import TkinterDnD

    from SeparationWorker.gui import (
        LANE_HEIGHT,
        PLAYHEAD_WIDTH,
        SEPARATION_CONTENT_TOP,
        SEPARATION_VIEW_HEIGHT,
        MixerViewModel,
        NAV_HEIGHT,
        SCREEN_MARGIN,
        SKIP_SECONDS,
        _draw_icon,
        StemslayerApp,
        lane_strip_height,
        mixer_view_height,
    )

    GUI_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - exercised only without a GUI stack
    GUI_IMPORT_ERROR = error

METAL_LANES = (
    "vocals.wav",
    "drums.wav",
    "bass.wav",
    "lead_guitar.wav",
    "rhythm_guitar.wav",
    "other.wav",
)


class FakeSession:
    sample_rate = 10
    frame_count = 100

    def peaks_for(self, _stem_name):
        return (0.0, 0.5, 0.25)


class GuiAppFixture:
    """Build one real window per test class, not per test.

    Building a window per test costs seconds each and loads the process
    enough to starve the wall-clock-bound playback suite that runs alongside
    it. One window is enough: every test resets the lane strip through the
    same call the constructor makes.
    """

    root = None
    app = None

    @classmethod
    def setUpClass(cls):
        if GUI_IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"GUI stack unavailable: {GUI_IMPORT_ERROR}")
        try:
            cls.root = ctk.CTk()
            # The same drag-and-drop bootstrap main() performs, so the window
            # under test is built exactly the way production builds it.
            TkinterDnD.require(cls.root)
        except Exception as error:
            cls.root = None
            raise unittest.SkipTest(f"No usable display or drag-and-drop stack: {error}")
        cls.root.withdraw()
        cls.app = StemslayerApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        # Cancel pending after() callbacks first. Left queued, they fire
        # against destroyed widgets and Tcl writes background errors that
        # would train a reader to ignore this suite's output.
        if cls.root is not None:
            try:
                for after_id in cls.root.tk.eval("after info").split():
                    cls.root.after_cancel(after_id)
            except Exception:
                pass
        # Close through the production path so the mixer controller releases
        # its worker and playback resources.
        if cls.app is not None:
            try:
                cls.app._close()
                cls.root = None
            except Exception:
                pass
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass
        cls.app = None
        cls.root = None

    def setUp(self):
        # The same call StemslayerApp makes when it first builds the strip.
        self.app._build_lanes(MixerViewModel().lane_rows)


class LaneRenderingTests(GuiAppFixture, unittest.TestCase):
    def state_for(self, lane_names, absent=()):
        return MixerState(
            folder=Path("stems"),
            session=FakeSession(),
            phase="ready",
            frame_count=100,
            settings=MixerSnapshot(tuple(MixSetting(name) for name in lane_names)),
            lane_names=tuple(lane_names),
            absent_lanes=tuple(absent),
        )

    def test_the_default_layout_builds_the_legacy_lanes(self):
        self.assertEqual(list(STEM_NAMES), list(self.app._lane_widgets))
        self.assertEqual(list(STEM_NAMES), list(self.app._waveform_canvases))

    def test_a_six_lane_result_rebuilds_the_strip_in_profile_order(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))

        self.assertEqual(list(METAL_LANES), list(self.app._lane_widgets))
        self.assertEqual(list(METAL_LANES), list(self.app._waveform_canvases))

    def test_returning_to_a_four_lane_result_shrinks_the_strip(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        self.app._render_mixer_state(self.state_for(STEM_NAMES))

        self.assertEqual(list(STEM_NAMES), list(self.app._lane_widgets))
        self.assertEqual(list(STEM_NAMES), list(self.app._waveform_canvases))

    def test_the_skip_icons_point_the_way_they_seek(self):
        """A forward arrow that points left is a lie the tests must catch."""
        canvas = tk.Canvas(self.root, width=40, height=40)
        self.addCleanup(canvas.destroy)

        for kind, points_right in (("forward", True), ("rewind", False)):
            _draw_icon(canvas, kind, 40, "#ffffff")
            arrows = canvas.find_withtag("icon")
            with self.subTest(icon=kind):
                self.assertEqual(2, len(arrows), f"{kind} should be a pair of arrows")
                for arrow in arrows:
                    base_x, _base_y, _x2, _y2, tip_x, _tip_y = canvas.coords(arrow)
                    if points_right:
                        self.assertGreater(tip_x, base_x, f"{kind} points the wrong way")
                    else:
                        self.assertLess(tip_x, base_x, f"{kind} points the wrong way")

    def test_the_skip_buttons_seek_in_the_direction_they_point(self):
        seeks = []
        self.app.mixer_controller.nudge = seeks.append

        self.app.rewind_button._command()
        self.app.forward_button._command()

        self.assertEqual([-SKIP_SECONDS, SKIP_SECONDS], seeks)

    def test_mute_and_solo_sit_next_to_each_other(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        self.mapped()
        widgets = self.app._lane_widgets["lead_guitar.wav"]

        gap = widgets["solo"].winfo_x() - (widgets["mute"].winfo_x() + widgets["mute"].winfo_width())

        self.assertLessEqual(gap, 8, f"mute and solo are {gap}px apart")
        self.assertGreaterEqual(gap, 0)

    def test_an_absent_lane_is_built_and_stays_controllable(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES, absent=("lead_guitar",)))

        self.assertIn("lead_guitar.wav", self.app._lane_widgets)
        widgets = self.app._lane_widgets["lead_guitar.wav"]
        self.assertEqual("normal", str(widgets["mute"].cget("state")))
        self.assertEqual("normal", str(widgets["scale"].cget("state")))

    def test_an_unchanged_layout_is_not_rebuilt(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        first = self.app._lane_widgets["lead_guitar.wav"]["scale"]

        self.app._render_mixer_state(self.state_for(METAL_LANES))

        self.assertIs(first, self.app._lane_widgets["lead_guitar.wav"]["scale"])

    def test_a_changed_absent_set_rebuilds_the_labels(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        before = self.app._lane_widgets["lead_guitar.wav"]["scale"]

        self.app._render_mixer_state(self.state_for(METAL_LANES, absent=("lead_guitar",)))

        self.assertIsNot(before, self.app._lane_widgets["lead_guitar.wav"]["scale"])

    def test_an_unknown_lane_renders_without_raising(self):
        self.app._render_mixer_state(self.state_for(("vocals.wav", "hammond_organ.wav")))

        self.assertEqual(
            ["vocals.wav", "hammond_organ.wav"], list(self.app._lane_widgets)
        )

    def assert_controls_are_reachable(self, lane_count: int):
        """Every lane control must sit inside the frame that clips it.

        Tk clips an oversized child silently, so a lane strip that shares one
        height between lanes loses its bottom row of controls as lanes are
        added, with no error anywhere. Only measurement catches that.
        """
        self.root.update_idletasks()
        for stem_name, widgets in self.app._lane_widgets.items():
            controls = widgets["controls"]
            with self.subTest(lanes=lane_count, lane=stem_name):
                self.assertEqual(LANE_HEIGHT, widgets["lane"].winfo_height())
                for control in ("mute", "solo", "scale"):
                    widget = widgets[control]
                    # Controls may sit in a sub-frame, so measure against the
                    # frame that actually clips them.
                    offset_y = 0 if widget.master is controls else widget.master.winfo_y()
                    offset_x = 0 if widget.master is controls else widget.master.winfo_x()
                    bottom = offset_y + widget.winfo_y() + widget.winfo_height()
                    right = offset_x + widget.winfo_x() + widget.winfo_width()
                    self.assertLessEqual(
                        bottom,
                        controls.winfo_height(),
                        f"{control} is clipped by {bottom - controls.winfo_height()}px at {lane_count} lanes",
                    )
                    self.assertLessEqual(
                        right,
                        controls.winfo_width(),
                        f"{control} overflows the controls by {right - controls.winfo_width()}px",
                    )

    def test_mute_and_solo_stay_visible_however_many_lanes_a_profile_publishes(self):
        self.app._show_view("mixer")
        for lane_count in (4, 6, 8):
            names = tuple(f"lane{index}.wav" for index in range(lane_count))
            self.app._render_mixer_state(self.state_for(names))
            self.assert_controls_are_reachable(lane_count)

    def height_requested_for(self, lane_names) -> int:
        """Return the window height the mixer asks for with this layout.

        The fixture window is withdrawn, so Tk never realises the geometry and
        the measured height stays at its default. What the mixer requests is
        the behaviour under test, so capture the request itself.
        """
        # The strip is only rebuilt when the layout actually changes, so move
        # off the target layout first to make the next render a real rebuild.
        self.app._render_mixer_state(self.state_for(("scratch.wav",)))
        requested = []
        original = self.root.geometry
        self.root.geometry = lambda spec=None, _o=original, _r=requested: (
            _r.append(spec) or _o(spec) if spec else _o()
        )
        try:
            self.app._render_mixer_state(self.state_for(lane_names))
        finally:
            self.root.geometry = original
        self.assertTrue(requested, "the mixer never sized itself for the new layout")
        return int(requested[-1].split("x")[1])

    def test_the_mixer_window_grows_with_the_lane_count(self):
        self.app._show_view("mixer")

        four = self.height_requested_for(STEM_NAMES)
        six = self.height_requested_for(METAL_LANES)

        self.assertEqual(six - four, mixer_view_height(6) - mixer_view_height(4))
        self.assertGreater(six, four)

    def test_the_mixer_title_reports_the_loaded_lane_count(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))

        self.assertIn("Six stems", self.app.mixer_title.get())

    def test_rebuilding_the_strip_keeps_the_container_scrollable(self):
        """The scrollbar is the safety net when the screen caps the window."""
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        self.app._render_mixer_state(self.state_for(STEM_NAMES))

        self.assertTrue(self.app._lanes_container._scrollbar.winfo_exists())
        self.assertTrue(self.app._lanes_container._parent_canvas.winfo_exists())

    def mapped(self):
        """Realise the withdrawn fixture window so geometry can be measured."""
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.addCleanup(self.root.withdraw)

    def test_the_playhead_is_one_line_that_crosses_the_whole_strip(self):
        self.app._show_view("mixer")
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        self.mapped()
        self.app._draw_playheads()
        self.root.update_idletasks()

        playhead = self.app._playhead

        self.assertTrue(playhead.winfo_ismapped(), "the playhead never reached the strip")
        self.assertEqual(PLAYHEAD_WIDTH, playhead.winfo_width())
        self.assertEqual(self.app._lanes_container.winfo_height(), playhead.winfo_height())
        # A line drawn per lane would leave strokes behind in the canvases.
        for canvas in self.app._waveform_canvases.values():
            self.assertEqual((), canvas.find_withtag("playhead"))

    def test_the_playhead_starts_where_the_waveforms_start_and_follows_the_position(self):
        self.app._show_view("mixer")
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        self.mapped()

        self.app._mixer_model = self.app._mixer_model.with_preview(0)
        self.app._draw_playheads()
        self.root.update_idletasks()
        start = self.app._playhead.winfo_x()

        self.app._mixer_model = self.app._mixer_model.with_preview(50)
        self.app._draw_playheads()
        self.root.update_idletasks()
        middle = self.app._playhead.winfo_x()

        canvas = next(iter(self.app._waveform_canvases.values()))
        self.assertEqual(canvas.winfo_x() + canvas.master.winfo_x(), start)
        self.assertGreater(middle, start)

    def test_the_playhead_survives_a_relayout(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        playhead = self.app._playhead

        self.app._render_mixer_state(self.state_for(STEM_NAMES))

        self.assertIs(playhead, self.app._playhead)
        self.assertTrue(playhead.winfo_exists())

    def test_mute_and_solo_come_before_the_lane_name(self):
        self.app._render_mixer_state(self.state_for(METAL_LANES))
        widgets = self.app._lane_widgets["lead_guitar.wav"]
        head = widgets["mute"].master
        label = next(
            child for child in head.winfo_children() if child not in (widgets["mute"], widgets["solo"])
        )

        mute_column = int(widgets["mute"].grid_info()["column"])
        solo_column = int(widgets["solo"].grid_info()["column"])

        self.assertLess(mute_column, solo_column)
        self.assertLess(solo_column, int(label.grid_info()["column"]))

    def test_the_window_is_tall_enough_that_no_lane_needs_scrolling(self):
        """The strip scrolls when it does not fit, so a short window hides lanes.

        This compares the height the mixer asks the window manager for against
        the height Tk says the view needs. Measuring the realised widgets
        instead would pass on a withdrawn test window whatever the request was.
        """
        self.app._show_view("mixer")
        available = self.root.winfo_screenheight() - SCREEN_MARGIN
        for lane_count in (4, 6):
            names = tuple(f"lane{index}.wav" for index in range(lane_count))
            requested = self.height_requested_for(names)
            needed = min(self.app._mixer_required_height() + NAV_HEIGHT + 1, available)
            with self.subTest(lanes=lane_count):
                self.assertGreaterEqual(
                    requested,
                    needed,
                    f"{lane_count} lanes need {needed}px but the window asks for "
                    f"{requested}px, so the strip has to scroll",
                )

    def test_the_chrome_is_what_the_view_needs_beyond_the_lane_strip(self):
        self.app._show_view("mixer")
        self.app._render_mixer_state(self.state_for(METAL_LANES))

        self.assertEqual(
            self.app._mixer_required_height(),
            self.app.mixer_chrome_height() + lane_strip_height(len(METAL_LANES)),
        )

    def test_the_transport_stays_disabled_until_a_session_is_loaded(self):
        self.app._render_mixer_state(MixerState())

        for name in ("rewind_button", "forward_button", "loop_button", "master_button"):
            with self.subTest(button=name):
                self.assertFalse(getattr(self.app, name)._enabled)
        self.assertEqual("disabled", str(self.app.master_slider.cget("state")))

    def test_the_transport_wakes_up_with_a_loaded_session(self):
        self.app._render_mixer_state(self.state_for(STEM_NAMES))

        for name in ("rewind_button", "forward_button", "loop_button", "master_button"):
            with self.subTest(button=name):
                self.assertTrue(getattr(self.app, name)._enabled)
        self.assertEqual("normal", str(self.app.master_slider.cget("state")))

    def test_the_loop_button_shows_that_looping_is_engaged(self):
        state = self.state_for(STEM_NAMES)
        self.app._render_mixer_state(state)
        self.assertFalse(self.app.loop_button._active)

        self.app._render_mixer_state(replace(state, looping=True))

        self.assertTrue(self.app.loop_button._active)

    def test_a_muted_master_shows_a_crossed_out_speaker(self):
        state = self.state_for(STEM_NAMES)

        self.app._render_mixer_state(replace(state, master_percent=0))

        self.assertEqual("volume_off", self.app.master_button._icon_kind)
        self.assertEqual(0, int(self.app.master_slider.get()))

        self.app._render_mixer_state(replace(state, master_percent=80))

        self.assertEqual("volume", self.app.master_button._icon_kind)
        self.assertEqual(80, int(self.app.master_slider.get()))

    def test_the_volume_button_remembers_the_level_it_silenced(self):
        self.app._master_volume_changed(60)

        self.assertEqual(60, self.app._master_percent_before_mute)

    def test_the_split_clock_reports_position_and_duration_separately(self):
        self.app._render_mixer_state(self.state_for(STEM_NAMES))

        self.assertEqual("00:00.00", self.app.mixer_position_time.get())
        self.assertEqual("00:10.00", self.app.mixer_duration_time.get())

    def test_mixer_settings_reach_the_rebuilt_role_lanes(self):
        state = self.state_for(METAL_LANES)
        muted = MixerSnapshot(
            tuple(
                MixSetting(name, gain=0.5, muted=(name == "lead_guitar.wav"))
                for name in METAL_LANES
            )
        )
        self.app._render_mixer_state(state)
        self.app._render_mixer_state(
            MixerState(
                folder=state.folder,
                session=state.session,
                phase="ready",
                frame_count=100,
                settings=muted,
                lane_names=METAL_LANES,
            )
        )

        percent = self.app._lane_widgets["rhythm_guitar.wav"]["percent"]
        self.assertEqual("50%", percent.cget("text"))


class ProfileSelectorTests(GuiAppFixture, unittest.TestCase):
    """The profile selector is the only way a user reaches the remediation."""

    def setUp(self):
        super().setUp()
        self.app.controller.set_profile(LEGACY_PROFILE_ID)

    def test_every_registered_profile_is_offered(self):
        offered = set(self.app.profile_selector.cget("values"))

        self.assertIn("Legacy", offered)
        self.assertIn("Metal Roles", offered)
        self.assertIn("Metal Stereo", offered)

    def test_the_selector_starts_on_the_available_profile(self):
        self.assertEqual("Legacy", self.app.profile_selector.get())
        self.assertEqual(4, len(self.app._chip_lane_ids))

    def test_selecting_metal_shows_the_remediation_and_blocks_starting(self):
        self.app._profile_selected("Metal Roles")

        self.assertEqual(METAL_PROFILE_ID, self.app.controller.state.profile_id)
        self.assertIn("admit_metal_guitar_model.py", self.app.status_detail.get())
        self.assertEqual("disabled", str(self.app.action.cget("state")))

    def test_selecting_metal_previews_its_six_channels(self):
        self.app._profile_selected("Metal Roles")

        self.assertEqual(
            ("vocals", "drums", "bass", "lead_guitar", "rhythm_guitar", "other"),
            self.app._chip_lane_ids,
        )

    def test_switching_back_restores_the_legacy_preview(self):
        self.app._profile_selected("Metal Roles")
        self.app._profile_selected("Legacy")

        self.assertEqual(("vocals", "drums", "bass", "other"), self.app._chip_lane_ids)
        self.assertEqual("Legacy", self.app.profile_selector.get())

    def test_metal_stereo_can_start_and_states_that_position_is_not_role(self):
        self.app.controller.set_input_file("song.mp3")

        self.app._profile_selected("Metal Stereo")

        self.assertTrue(self.app.controller.state.profile_available)
        self.assertEqual("normal", str(self.app.action.cget("state")))
        detail = self.app.status_detail.get()
        self.assertIn("position is not role", detail)
        self.assertIn("centre", detail)

    def test_metal_stereo_previews_position_lanes_never_roles(self):
        self.app._profile_selected("Metal Stereo")

        self.assertEqual(
            ("vocals", "drums", "bass", "guitar_center", "guitar_sides", "other"),
            self.app._chip_lane_ids,
        )
        labels = [row[1] for row in self.app._mixer_model.lane_rows]
        self.assertNotIn("LEAD GUITAR", labels)

    def test_an_unknown_selection_is_ignored(self):
        self.app._profile_selected("Nonexistent")

        self.assertEqual(LEGACY_PROFILE_ID, self.app.controller.state.profile_id)

    def test_the_hero_copy_promises_the_selected_channel_count(self):
        self.assertIn("Four clean channels", self.app.hero_headline.get())
        self.assertIn("four clean local stems", self.app.hero_subtitle.get())

        self.app._profile_selected("Metal Roles")

        self.assertIn("Six clean channels", self.app.hero_headline.get())
        self.assertIn("six clean local stems", self.app.hero_subtitle.get())

    def test_the_start_button_reports_the_selected_channel_count(self):
        self.app._profile_selected("Metal Roles")

        self.assertEqual("Separate into 6 stems", self.app.action.cget("text"))

    def test_every_profile_fits_inside_the_separation_window(self):
        """The placed content clips instead of scrolling.

        A profile whose content overflows silently hides the start button, so
        the user cannot separate anything at all. This measures the tallest
        profile rather than trusting an estimate.
        """
        self.app._show_view("separation")
        for display_name in self.app._profile_choices:
            with self.subTest(profile=display_name):
                self.app._profile_selected(display_name)
                self.root.update_idletasks()
                bottom = SEPARATION_CONTENT_TOP + self.app._separation_center.winfo_reqheight()

                self.assertLessEqual(
                    bottom,
                    SEPARATION_VIEW_HEIGHT,
                    f"{display_name} content overflows the view by {bottom - SEPARATION_VIEW_HEIGHT}px "
                    "and would hide the start button",
                )


if __name__ == "__main__":
    unittest.main()
