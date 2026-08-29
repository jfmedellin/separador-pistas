"""Real-widget coverage for the mixer lane strip.

These tests build an actual Tk window, so they prove the lane strip is
rebuilt from the published layout rather than trusting the headless view
model alone. They skip themselves when no display is available.
"""

import unittest
from pathlib import Path

from SeparationWorker.engine.mixer import MixSetting, MixerSnapshot
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.mixer_controller import MixerState

try:
    import customtkinter as ctk
    from tkinterdnd2 import TkinterDnD

    from SeparationWorker.gui import MixerViewModel, StemslayerApp

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


class LaneRenderingTests(unittest.TestCase):
    """One real window is built for the whole class.

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


if __name__ == "__main__":
    unittest.main()
