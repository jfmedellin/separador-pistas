"""Regression tests for ARC-01 single-instance lock exclusivity.

`acquire_single_instance()` MUST use an OS-managed exclusive lock so a
second launch is refused while a first launch is running, and the lock
MUST release automatically once the holder process ends -- including an
abnormal termination -- without relying on a presence-only sentinel file.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import SeparationWorker.instance_lock as instance_lock
from SeparationWorker import gui
from SeparationWorker.instance_lock import acquire_single_instance

REPO_ROOT = Path(__file__).resolve().parents[2]


def _acquire_in_subprocess(root: Path) -> subprocess.CompletedProcess:
    """Spawn a fresh interpreter that calls acquire_single_instance(root) once and exits."""
    script = (
        "from pathlib import Path\n"
        "from SeparationWorker.instance_lock import acquire_single_instance\n"
        f"print(acquire_single_instance(Path({str(root)!r})))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


class InstanceLockExclusivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        # An in-process acquisition intentionally keeps its handle open for
        # this process's entire lifetime (D9), so this test process itself
        # must release it before the temp root can be removed on Windows.
        held = instance_lock._held_lock_handle
        if held is not None:
            held.close()
            instance_lock._held_lock_handle = None
        self.temp.cleanup()

    def test_second_process_is_refused_while_first_still_holds_the_lock(self):
        # This test process holds the lock for the whole test method, so the
        # spawned subprocess is genuinely the second instance racing a live holder.
        acquired = acquire_single_instance(self.root)
        self.assertTrue(acquired)

        second = _acquire_in_subprocess(self.root)

        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertEqual(second.stdout.strip(), "False")

    def test_lock_releases_once_the_holder_process_exits(self):
        first = _acquire_in_subprocess(self.root)
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(first.stdout.strip(), "True")

        # The subprocess has already terminated by the time subprocess.run
        # returns, so the OS has released its lock on this exact root.
        acquired = acquire_single_instance(self.root)

        self.assertTrue(acquired)


class MainGatesHistoryAccessOnLockTests(unittest.TestCase):
    """`main()` MUST decide the lock before any HistoryStore/CustomTkinter setup."""

    def test_second_instance_never_constructs_history_or_recovers(self):
        with mock.patch("SeparationWorker.gui.acquire_single_instance", return_value=False) as acquire, \
                mock.patch("SeparationWorker.gui.HistoryStore") as history_store_cls, \
                mock.patch("SeparationWorker.gui.StemslayerApp") as app_cls, \
                mock.patch("SeparationWorker.gui.ctk.set_appearance_mode") as set_mode, \
                mock.patch("SeparationWorker.gui.ctk.CTk") as ctk_ctk, \
                mock.patch("SeparationWorker.gui.TkinterDnD.require") as dnd_require, \
                mock.patch("SeparationWorker.gui.messagebox.showerror") as showerror, \
                mock.patch("SeparationWorker.gui.tk.Tk") as tk_tk:
            fake_dialog_root = mock.Mock()
            tk_tk.return_value = fake_dialog_root

            exit_code = gui.main([])

        acquire.assert_called_once()
        history_store_cls.assert_not_called()
        app_cls.assert_not_called()
        set_mode.assert_not_called()
        ctk_ctk.assert_not_called()
        dnd_require.assert_not_called()
        fake_dialog_root.withdraw.assert_called_once()
        showerror.assert_called_once()
        fake_dialog_root.destroy.assert_called_once()
        self.assertEqual(exit_code, 1)

    def test_normal_startup_proceeds_when_lock_is_acquired(self):
        with mock.patch("SeparationWorker.gui.acquire_single_instance", return_value=True) as acquire, \
                mock.patch("SeparationWorker.gui.HistoryStore") as history_store_cls, \
                mock.patch("SeparationWorker.gui.StemslayerApp") as app_cls, \
                mock.patch("SeparationWorker.gui.ctk.set_appearance_mode") as set_mode, \
                mock.patch("SeparationWorker.gui.ctk.CTk") as ctk_ctk, \
                mock.patch("SeparationWorker.gui.TkinterDnD.require") as dnd_require:
            fake_root = mock.Mock()
            ctk_ctk.return_value = fake_root

            exit_code = gui.main([])

        acquire.assert_called_once()
        set_mode.assert_called_once_with("dark")
        ctk_ctk.assert_called_once()
        dnd_require.assert_called_once_with(fake_root)
        app_cls.assert_called_once_with(fake_root)
        history_store_cls.assert_not_called()  # HistoryStore() lives inside StemslayerApp, which is mocked here
        fake_root.mainloop.assert_called_once()
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
