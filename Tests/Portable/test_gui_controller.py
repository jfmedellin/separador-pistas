import unittest
from pathlib import Path

from SeparationWorker.demucs_adapter import DemucsSeparationError
from SeparationWorker.gui_controller import SeparationController


class QueuedExecution:
    def __init__(self):
        self.workers = []
        self.events = []

    def start_worker(self, callback):
        self.workers.append(callback)

    def dispatch(self, callback):
        self.events.append(callback)


class GuiControllerTests(unittest.TestCase):
    def configured(self, separate):
        queue = QueuedExecution()
        states = []
        controller = SeparationController(
            separate=separate,
            start_worker=queue.start_worker,
            dispatch=queue.dispatch,
            on_change=states.append,
        )
        controller.set_input_file("song.mp3")
        controller.set_output_directory("exports/song-stems")
        return controller, queue, states

    def test_requires_both_paths_before_starting(self):
        queue = QueuedExecution()
        controller = SeparationController(start_worker=queue.start_worker)

        self.assertFalse(controller.start())

        self.assertEqual("error", controller.state.phase)
        self.assertIn("Select both paths", controller.state.detail)
        self.assertEqual([], queue.workers)

    def test_runs_separation_in_worker_and_completes_through_dispatch(self):
        calls = []

        def separate(input_file, output_directory):
            calls.append((input_file, output_directory))
            return Path(output_directory)

        controller, queue, _states = self.configured(separate)

        self.assertTrue(controller.start())
        self.assertEqual("running", controller.state.phase)
        self.assertEqual([], calls)
        queue.workers.pop()()
        self.assertEqual([("song.mp3", "exports/song-stems")], calls)
        self.assertEqual("running", controller.state.phase)
        queue.events.pop()()

        self.assertEqual("success", controller.state.phase)
        self.assertEqual("4 stems are ready", controller.state.headline)
        self.assertEqual(str(Path("exports/song-stems")), controller.state.detail)

    def test_dispatches_successful_result_path_once_through_injected_callback(self):
        results = []

        def separate(_input_file, output_directory):
            return Path(output_directory)

        queue = QueuedExecution()
        controller = SeparationController(
            separate=separate,
            start_worker=queue.start_worker,
            dispatch=queue.dispatch,
            on_success=results.append,
        )
        controller.set_input_file("song.mp3")
        controller.set_output_directory("exports/song-stems")

        self.assertTrue(controller.start())
        queue.workers.pop()()
        self.assertEqual([], results)
        queue.events.pop()()

        self.assertEqual([Path("exports/song-stems")], results)
        self.assertEqual("success", controller.state.phase)

    def test_prevents_duplicate_runs_and_selection_changes_while_running(self):
        controller, queue, _states = self.configured(lambda _input, output: Path(output))

        self.assertTrue(controller.start())
        self.assertFalse(controller.start())
        self.assertFalse(controller.set_input_file("other.mp3"))

        self.assertEqual(1, len(queue.workers))
        self.assertEqual("song.mp3", controller.state.input_file)

    def test_surfaces_actionable_backend_error_on_ui_dispatch(self):
        def fail(_input, _output):
            raise DemucsSeparationError(
                "demucs.output_incomplete",
                "The bass stem is missing.",
                "Inspect Demucs diagnostics and retry the complete song.",
            )

        controller, queue, _states = self.configured(fail)
        controller.start()
        queue.workers.pop()()
        queue.events.pop()()

        self.assertEqual("error", controller.state.phase)
        self.assertEqual("Separation failed", controller.state.headline)
        self.assertIn("bass stem is missing", controller.state.detail)
        self.assertIn("retry the complete song", controller.state.detail)
        self.assertTrue(controller.state.can_start)


if __name__ == "__main__":
    unittest.main()
