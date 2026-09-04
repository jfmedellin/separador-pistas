"""Real-widget coverage for the split-library history rows.

These tests build an actual Tk window and click the real Open/Remove
buttons the way a user would, rather than calling the controller methods
directly. A row's fixed-width text columns (title/artist/detail) can
request more horizontal space than the row actually has; before the fix
in _render_library, Tk's packer sacrificed whichever widgets were packed
last -- the action buttons -- squeezing them to a sliver no real click
could land on. The actions are icon-only circular buttons (_RoundButton),
tagged on their frame with a `library_action` marker so a test can find
"the Open button" or "the Remove button" without relying on button text.
They skip themselves when no display is available.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from SeparationWorker.engine.pcm import PlanarPCM
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.engine.wav import encode_float32_wav
from SeparationWorker.history import HistoryStore
from SeparationWorker.job_manager import JobCancelled, current_job

try:
    import customtkinter as ctk
    from tkinterdnd2 import TkinterDnD

    from SeparationWorker.gui import StemslayerApp

    GUI_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - exercised only without a GUI stack
    GUI_IMPORT_ERROR = error


def write_stems(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(STEM_NAMES):
        audio = PlanarPCM(8_000, ((0.1 + index * 0.01, -0.1, 0.05, -0.05),))
        (folder / name).write_bytes(encode_float32_wav(audio))


class LibraryRowFixture(unittest.TestCase):
    """Build one real window per test class; isolate it from the real user library.

    StemslayerApp constructs its own HistoryStore against %LOCALAPPDATA%, so
    every test here redirects that variable to a throwaway directory instead
    of touching the developer's actual saved history.
    """

    root = None
    app = None
    _localappdata_dir = None
    _previous_localappdata = None

    @classmethod
    def setUpClass(cls):
        if GUI_IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"GUI stack unavailable: {GUI_IMPORT_ERROR}")
        cls._localappdata_dir = tempfile.TemporaryDirectory()
        cls._previous_localappdata = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = cls._localappdata_dir.name
        try:
            ctk.set_appearance_mode("dark")
            cls.root = ctk.CTk()
            TkinterDnD.require(cls.root)
            cls.root.geometry("1200x800+50+50")
        except Exception as error:
            cls.root = None
            cls._restore_localappdata()
            raise unittest.SkipTest(f"No usable display or drag-and-drop stack: {error}")
        cls.root.withdraw()
        cls.app = StemslayerApp(cls.root)

    @classmethod
    def _restore_localappdata(cls):
        if cls._previous_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = cls._previous_localappdata
        if cls._localappdata_dir is not None:
            cls._localappdata_dir.cleanup()
            cls._localappdata_dir = None

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                for after_id in cls.root.tk.eval("after info").split():
                    cls.root.after_cancel(after_id)
            except Exception:
                pass
        if cls.app is not None:
            try:
                cls.app._close()
            except Exception:
                pass
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass
        cls.app = None
        cls.root = None
        cls._restore_localappdata()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self._clear_rows)
        self.app.mixer_controller.unload()
        self._clear_rows()

    def _clear_rows(self):
        for record in self.app.history_store.query():
            if record.status in {"preparing", "processing"}:
                self.app.history_store.update(record.track_id, status="failed")
            self.app.history_store.remove(record.track_id)
        self.app.library_controller.refresh()

    def make_ready_track(self, *, title, artist, corrupt_result=False):
        source = Path(self.temp.name) / f"{title}.flac"
        source.write_bytes(b"fake-source-audio")
        record = self.app.history_store.create(source, LEGACY_PROFILE)
        if not corrupt_result:
            write_stems(record.result_directory)
        record = self.app.history_store.update(
            record.track_id, title=title, artist=artist, status="ready", duration_seconds=200.0
        )
        self.app.library_controller.refresh()
        return record

    def render_and_find_row(self):
        self.app._show_view("separation")
        self.root.update_idletasks()
        self.root.update()
        rows = self.app.library_rows.winfo_children()
        self.assertEqual(1, len(rows), "expected exactly one rendered library row")
        return rows[0].winfo_children()[0]  # the row's `body` frame

    @staticmethod
    def find_action_button(body, action):
        """Find a row's action button by its `library_action` marker (open/retry/remove).

        The action buttons are `_RoundButton` wrappers, not plain widgets with
        a `text` option, so they can't be found by `cget("text")`; instead the
        frame `_render_library` packs is tagged with the wrapper it belongs to.
        """
        for child in body.winfo_children():
            if getattr(child, "library_action", None) == action:
                return child.library_button
        return None

    def pump_until(self, predicate, *, timeout=5.0):
        """Load/unload cross a real background thread; give it wall-clock time to land."""
        deadline = time.monotonic() + timeout
        while not predicate():
            self.root.update()
            if time.monotonic() > deadline:
                self.fail("timed out waiting for the mixer controller to settle")
            time.sleep(0.01)


class ReadyRowOpenTests(LibraryRowFixture):
    def test_open_button_has_real_clickable_width_next_to_a_long_title(self):
        # Regression: with realistic title/artist text, the fixed-width text
        # columns alone (210+145+235 px) already exceed the row's actual body
        # width, so before the fix Tk squeezed the action buttons to ~1px.
        self.make_ready_track(
            title="Bohemian Rhapsody (Remastered 2011 Extended Mix)",
            artist="Queen featuring The Muppets Orchestra & Chorus",
        )
        body = self.render_and_find_row()

        open_button = self.find_action_button(body, "open")
        remove_button = self.find_action_button(body, "remove")

        self.assertIsNotNone(open_button)
        self.assertIsNotNone(remove_button)
        self.assertTrue(open_button.frame.winfo_ismapped())
        self.assertTrue(remove_button.frame.winfo_ismapped())
        self.assertGreaterEqual(open_button.frame.winfo_width(), 32)
        self.assertGreaterEqual(remove_button.frame.winfo_width(), 32)
        self.assertLessEqual(
            open_button.frame.winfo_rootx() + open_button.frame.winfo_width(),
            remove_button.frame.winfo_rootx(),
            "Open and Remove buttons overlap",
        )

    def test_clicking_open_on_a_ready_row_switches_to_mixer_with_its_stems(self):
        record = self.make_ready_track(title="Song", artist="Artist")
        body = self.render_and_find_row()
        open_button = self.find_action_button(body, "open")

        open_button.invoke()
        self.pump_until(lambda: self.app.mixer_controller.state.phase != "loading")

        self.assertEqual("mixer", self.app._view)
        self.assertEqual(record.result_directory.resolve(), Path(self.app.mixer_controller.state.folder).resolve())
        self.assertEqual("ready", self.app.mixer_controller.state.phase)

    def test_clicking_open_on_a_missing_result_marks_the_row_unavailable_without_switching_view(self):
        self.make_ready_track(title="Ghost Track", artist="Artist", corrupt_result=True)
        body = self.render_and_find_row()
        open_button = self.find_action_button(body, "open")

        open_button.invoke()
        self.root.update_idletasks()
        self.root.update()

        self.assertEqual("separation", self.app._view)
        tracks = self.app.library_controller.state.tracks
        self.assertEqual(1, len(tracks))
        self.assertEqual("unavailable", tracks[0].status)


class LibraryDropTargetTests(LibraryRowFixture):
    """Regression: once the catalog replaced the empty-state drop zone, no
    library widget accepted DND_FILES, so dragging a song in did nothing."""

    def has_drop_binding(self, widget):
        # CTk widgets redirect .bind() to their inner canvas, so ask Tk about
        # the frame itself -- that is the widget tkdnd delivers <<Drop>> to.
        return bool(self.root.tk.call("bind", widget._w, "<<Drop>>"))

    def test_library_panel_and_rendered_rows_accept_file_drops(self):
        self.make_ready_track(title="Song", artist="Artist")
        body = self.render_and_find_row()

        self.assertTrue(self.has_drop_binding(self.app._library_panel))
        self.assertTrue(self.has_drop_binding(self.app.library_rows))
        self.assertTrue(self.has_drop_binding(body))
        self.assertTrue(self.has_drop_binding(self.find_action_button(body, "open").frame))

    def test_rows_rendered_later_are_registered_without_rebinding_the_panel(self):
        self.make_ready_track(title="First", artist="Artist")
        self.render_and_find_row()
        panel_binding = self.root.tk.call("bind", self.app._library_panel._w, "<<Drop>>")

        self.make_ready_track(title="Second", artist="Artist")
        self.app._show_view("separation")
        self.root.update_idletasks()
        self.root.update()
        rows = [row for row in self.app.library_rows.winfo_children() if row.winfo_children()]

        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertTrue(self.has_drop_binding(row.winfo_children()[0]))
        self.assertEqual(panel_binding, self.root.tk.call("bind", self.app._library_panel._w, "<<Drop>>"))

    def test_a_drop_on_the_library_routes_to_the_profile_dialog(self):
        self.make_ready_track(title="Song", artist="Artist")
        self.render_and_find_row()
        chosen = []
        original = self.app._choose_profile
        self.app._choose_profile = chosen.append
        self.addCleanup(setattr, self.app, "_choose_profile", original)

        class Event:
            data = "{C:/Music/new song.mp3}"

        self.app._drop_input(Event())

        self.assertEqual(["C:/Music/new song.mp3"], chosen)


class ReadyRowRemoveTests(LibraryRowFixture):
    def test_clicking_remove_on_a_ready_row_deletes_the_managed_folder_but_keeps_the_source(self):
        record = self.make_ready_track(title="Song", artist="Artist")
        body = self.render_and_find_row()
        remove_button = self.find_action_button(body, "remove")

        self.assertTrue(remove_button.is_enabled())
        remove_button.invoke()
        self.root.update_idletasks()
        self.root.update()

        self.assertEqual(0, len(self.app.library_controller.state.tracks))
        self.assertFalse(record.result_directory.exists())
        self.assertTrue(Path(record.source_path).exists())
        self.assertEqual(0, len(self.app.library_rows.winfo_children()))

    def test_removing_a_track_open_in_the_mixer_unloads_it_before_deleting(self):
        record = self.make_ready_track(title="Playing Now", artist="Artist")
        body = self.render_and_find_row()
        self.find_action_button(body, "open").invoke()
        self.pump_until(lambda: self.app.mixer_controller.state.phase != "loading")
        self.assertEqual(record.result_directory.resolve(), Path(self.app.mixer_controller.state.folder).resolve())

        body = self.render_and_find_row()
        remove_button = self.find_action_button(body, "remove")
        remove_button.invoke()
        self.root.update_idletasks()
        self.root.update()

        self.assertIsNone(self.app.mixer_controller.state.folder)
        self.assertFalse(record.result_directory.exists())
        self.assertEqual(0, len(self.app.library_controller.state.tracks))

    def test_remove_button_is_disabled_while_preparing_or_processing(self):
        source = Path(self.temp.name) / "still-working.wav"
        source.write_bytes(b"audio")
        record = self.app.history_store.create(source, LEGACY_PROFILE)
        self.app.library_controller.refresh()
        body = self.render_and_find_row()
        remove_button = self.find_action_button(body, "remove")

        self.assertFalse(remove_button.is_enabled())
        self.assertFalse(self.app.history_store.remove(record.track_id))
        self.assertIsNotNone(self.app.history_store.get(record.track_id))

        self.app.history_store.update(record.track_id, status="processing")
        self.app.library_controller.refresh()
        body = self.render_and_find_row()
        remove_button = self.find_action_button(body, "remove")
        self.assertFalse(remove_button.is_enabled())


class CloseDrainTests(LibraryRowFixture):
    """Regression for the design's "Close while jobs are queued" sequence:
    _close() must drain (cancel + join, bounded by JobManager.shutdown's
    deadline) before root.destroy() fires, and no owned child process may
    survive past that bounded deadline. Must fail on current master, where
    _close() destroys the window immediately with no drain at all.
    """

    def test_close_kills_the_running_child_process_and_destroys_only_after_the_drain_completes(self):
        started = threading.Event()
        process_holder: dict = {}

        def blocking_separate(input_path, result_directory, *, profile, on_progress, cancellation, **_extra):
            handle = current_job()
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            process_holder["process"] = process
            handle.register(process)
            started.set()
            try:
                process.wait()
            finally:
                handle.unregister(process)
            if cancellation is not None and cancellation.cancelled:
                raise JobCancelled("job cancelled mid separation")
            write_stems(Path(result_directory))
            return Path(result_directory)

        self.app.library_controller._separate = blocking_separate
        source = Path(self.temp.name) / "blocking.wav"
        source.write_bytes(b"fake-source-audio")
        self.app.library_controller.add(source, LEGACY_PROFILE.profile_id)
        self.assertTrue(started.wait(timeout=5), "the blocking job never started its child process")

        order = []
        original_shutdown = self.app.library_controller.shutdown
        original_destroy = self.root.destroy

        def spy_shutdown(*args, **kwargs):
            order.append("shutdown_start")
            result = original_shutdown(*args, **kwargs)
            order.append("shutdown_end")
            return result

        def spy_destroy(*args, **kwargs):
            order.append("destroy")
            return original_destroy(*args, **kwargs)

        self.app.library_controller.shutdown = spy_shutdown
        self.root.destroy = spy_destroy

        with mock.patch("SeparationWorker.gui.messagebox.askyesno", return_value=True) as confirm:
            self.app._close()

        confirm.assert_called_once()
        self.assertEqual(
            ["shutdown_start", "shutdown_end", "destroy"], order,
            "root.destroy() must fire only after the drain completes, never before",
        )
        process = process_holder.get("process")
        self.assertIsNotNone(process, "the blocking job never registered its child process")
        self.assertIsNotNone(
            process.poll(), "no child process may survive close (bounded by JobManager.shutdown's deadline)"
        )


class CloseConfirmationDeclinedTests(LibraryRowFixture):
    """D11: a declined close-confirmation must leave the app open and running."""

    def test_declined_confirmation_leaves_closed_false_and_the_job_running(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_separate(input_path, result_directory, *, profile, on_progress, cancellation, **_extra):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("timed out waiting to be released")
            write_stems(Path(result_directory))
            return Path(result_directory)

        self.app.library_controller._separate = slow_separate
        source = Path(self.temp.name) / "declined.wav"
        source.write_bytes(b"fake-source-audio")
        record = self.app.library_controller.add(source, LEGACY_PROFILE.profile_id)
        self.assertTrue(entered.wait(timeout=5), "the slow job never started")

        with mock.patch("SeparationWorker.gui.messagebox.askyesno", return_value=False) as confirm:
            self.app._close()
            confirm.assert_called_once()

        self.assertFalse(self.app._closed, "a declined confirmation must leave _closed False")
        self.assertTrue(self.root.winfo_exists(), "a declined confirmation must not destroy the window")

        release.set()
        self.pump_until(lambda: self.app.history_store.get(record.track_id).status == "ready")


class QueuedLabelRenderTests(LibraryRowFixture):
    """CRITICAL gap (verify-report): no test exercised the derived "Queued"
    label render path in gui.py's _render_library, fed by
    SplitLibraryController.state.queued_track_ids. Uses the real
    StemslayerApp, a real Tk root, and real background threads -- only the
    subprocess-launch boundary (`_separate`) is faked, exactly like
    CloseDrainTests/CloseConfirmationDeclinedTests above.
    """

    def test_queued_row_renders_the_queued_label_while_its_status_stays_preparing(self):
        # Every staged input copy is renamed to "source<ext>" (ARC-02's
        # immutable-copy staging), so jobs are distinguished by call order,
        # not by the input path's name.
        entered = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]
        started: list[int] = []

        def blocking_separate(input_path, result_directory, *, profile, on_progress, cancellation, **_extra):
            index = len(started)
            started.append(index)
            entered[index].set()
            if not release[index].wait(timeout=10):
                raise RuntimeError(f"timed out waiting to release job {index}")
            write_stems(Path(result_directory))
            return Path(result_directory)

        self.app.library_controller._separate = blocking_separate

        def add_source(name):
            path = Path(self.temp.name) / f"{name}.wav"
            path.write_bytes(f"audio-{name}".encode())
            return self.app.library_controller.add(path, LEGACY_PROFILE.profile_id)

        running = add_source("running")
        self.assertTrue(entered[0].wait(timeout=5), "the running job never started")

        add_source("queued-0")
        third = add_source("queued-1")

        # queued-1 is already reported the moment it is submitted (D4 fires
        # on_queue_change on every submit, not only on promotion). Release
        # the running job anyway so queued-0 starts and queued-1 becomes the
        # sole remaining queued row -- that is the row that must render
        # "Queued" once the label has to reflect an actual state change, not
        # just its initial submit-time snapshot.
        release[0].set()
        self.assertTrue(entered[1].wait(timeout=5), "queued-0 was never promoted")

        self.pump_until(
            lambda: third.track_id in self.app.library_controller.state.queued_track_ids,
            timeout=5.0,
        )

        preparing = self.app.history_store.get(third.track_id)
        self.assertEqual("preparing", preparing.status)

        self.app._show_view("separation")
        self.root.update_idletasks()
        self.root.update()

        # _render_library packs a hairline divider frame as a bare sibling
        # between rows (no nested body), so only frames with children are
        # actual rows.
        rows = [row for row in self.app.library_rows.winfo_children() if row.winfo_children()]
        target_body = None
        for row in rows:
            body = row.winfo_children()[0]
            titles = [
                child.cget("text") for child in body.winfo_children() if isinstance(child, ctk.CTkLabel)
            ]
            if third.title in titles:
                target_body = body
                break
        self.assertIsNotNone(target_body, "could not find the queued row by title")

        labels = [
            child.cget("text") for child in target_body.winfo_children() if isinstance(child, ctk.CTkLabel)
        ]
        self.assertIn("Queued", labels, "a queued row must render the derived Queued label")

        release[1].set()
        release[2].set()
        self.pump_until(lambda: self.app.history_store.get(running.track_id).status == "ready")
        self.pump_until(lambda: self.app.history_store.get(third.track_id).status == "ready")


if __name__ == "__main__":
    unittest.main()
