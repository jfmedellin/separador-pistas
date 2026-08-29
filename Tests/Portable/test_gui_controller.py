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
    def configured(self, separate, *, cache_directory=None):
        queue = QueuedExecution()
        states = []
        kwargs = {}
        if cache_directory is not None:
            kwargs["cache_directory"] = cache_directory
        controller = SeparationController(
            separate=separate,
            start_worker=queue.start_worker,
            dispatch=queue.dispatch,
            on_change=states.append,
            **kwargs,
        )
        controller.set_input_file("song.mp3")
        return controller, queue, states

    def test_set_input_file_derives_result_directory_via_injected_cache_directory(self):
        calls = []

        def cache_directory(path):
            calls.append(path)
            return Path("cache") / "abc123"

        queue = QueuedExecution()
        controller = SeparationController(start_worker=queue.start_worker, cache_directory=cache_directory)

        self.assertTrue(controller.set_input_file("song.mp3"))

        self.assertEqual(["song.mp3"], calls)
        self.assertEqual(Path("cache") / "abc123", controller.state.result_directory)

    def test_can_start_is_true_on_input_alone(self):
        queue = QueuedExecution()
        controller = SeparationController(start_worker=queue.start_worker, cache_directory=lambda path: Path("cache"))

        self.assertFalse(controller.state.can_start)
        controller.set_input_file("song.mp3")
        self.assertTrue(controller.state.can_start)

    def test_start_error_path_is_only_for_missing_input(self):
        queue = QueuedExecution()
        controller = SeparationController(start_worker=queue.start_worker)

        self.assertFalse(controller.start())

        self.assertEqual("error", controller.state.phase)
        self.assertEqual("Choose an input audio file", controller.state.headline)
        self.assertNotIn("result folder", controller.state.detail.lower())
        self.assertNotIn("output directory", controller.state.detail.lower())
        self.assertEqual([], queue.workers)

    def test_runs_separation_in_worker_with_derived_directory_and_completes_through_dispatch(self):
        calls = []

        def separate(input_file, result_directory):
            calls.append((input_file, result_directory))
            return result_directory

        controller, queue, _states = self.configured(
            separate, cache_directory=lambda path: Path("cache") / "song-key"
        )

        self.assertTrue(controller.start())
        self.assertEqual("running", controller.state.phase)
        self.assertEqual([], calls)
        queue.workers.pop()()
        self.assertEqual([("song.mp3", Path("cache") / "song-key")], calls)
        self.assertEqual("running", controller.state.phase)
        queue.events.pop()()

        self.assertEqual("success", controller.state.phase)
        self.assertEqual("4 stems are ready", controller.state.headline)
        self.assertEqual(str(Path("cache") / "song-key"), controller.state.detail)

    def test_dispatches_successful_result_path_once_through_injected_callback(self):
        results = []

        def separate(_input_file, result_directory):
            return Path(result_directory)

        queue = QueuedExecution()
        controller = SeparationController(
            separate=separate,
            cache_directory=lambda path: Path("cache") / "song-key",
            start_worker=queue.start_worker,
            dispatch=queue.dispatch,
            on_success=results.append,
        )
        controller.set_input_file("song.mp3")

        self.assertTrue(controller.start())
        queue.workers.pop()()
        self.assertEqual([], results)
        queue.events.pop()()

        self.assertEqual([Path("cache") / "song-key"], results)
        self.assertEqual("success", controller.state.phase)

    def test_prevents_duplicate_runs_and_selection_changes_while_running(self):
        controller, queue, _states = self.configured(
            lambda _input, directory: Path(directory), cache_directory=lambda path: Path("cache")
        )

        self.assertTrue(controller.start())
        self.assertFalse(controller.start())
        self.assertFalse(controller.set_input_file("other.mp3"))

        self.assertEqual(1, len(queue.workers))
        self.assertEqual("song.mp3", controller.state.input_file)

    def test_surfaces_actionable_backend_error_on_ui_dispatch(self):
        def fail(_input, _directory):
            raise DemucsSeparationError(
                "demucs.output_incomplete",
                "The bass stem is missing.",
                "Inspect Demucs diagnostics and retry the complete song.",
            )

        controller, queue, _states = self.configured(fail, cache_directory=lambda path: Path("cache"))
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
