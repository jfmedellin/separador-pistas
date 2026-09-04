import subprocess
import sys
import threading
import time
import unittest

from SeparationWorker.job_manager import (
    JobCancelled,
    JobManager,
    current_job,
    run_owned,
)


class FakeProcess:
    """Popen-shaped fake: `.terminate()`/`.kill()`/`.wait(timeout)`, no real OS process.

    `exits_after` names which call makes the process exit: None (never exits
    on its own), "terminate" (exits the moment `.terminate()` is called, i.e.
    well within any grace period), or "kill" (only `.kill()` makes it exit).
    """

    def __init__(self, *, exits_after=None):
        self.exits_after = exits_after
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self._exited = False

    def terminate(self):
        self.terminate_calls += 1
        if self.exits_after == "terminate":
            self._exited = True

    def kill(self):
        self.kill_calls += 1
        if self.exits_after in ("terminate", "kill"):
            self._exited = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._exited:
            return 0
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


class JobManagerQueueTests(unittest.TestCase):
    def test_fifth_submit_at_default_concurrency_queues_and_starts_in_fifo_order(self):
        bodies = []
        queue_snapshots = []
        manager = JobManager(start_worker=bodies.append, on_queue_change=queue_snapshots.append)
        started = []

        def make_run(job_id):
            def run(_handle):
                started.append(job_id)
            return run

        for job_id in ("a", "b", "c", "d", "e"):
            manager.submit(job_id, make_run(job_id))

        # Concurrency 1: only the first job is ever handed to start_worker up
        # front; the other four never reject and simply wait in memory.
        self.assertEqual(1, len(bodies))
        self.assertEqual(("b", "c", "d", "e"), manager.queued_ids())

        # Draining one body at a time proves strict FIFO start order and that
        # on_queue_change always reports the exact remaining order.
        while bodies:
            bodies.pop(0)()

        self.assertEqual(["a", "b", "c", "d", "e"], started)
        # Every submit() reports the queue immediately (including "a"'s own
        # transient (\"a\",) before it is promoted) so a job never sits
        # queued with no reported position; each later drain reports the
        # exact remaining FIFO order as jobs are promoted in turn.
        self.assertEqual(
            [
                ("a",), (),
                ("b",), ("b", "c"), ("b", "c", "d"), ("b", "c", "d", "e"),
                ("c", "d", "e"), ("d", "e"), ("e",), (),
            ],
            queue_snapshots,
        )
        self.assertEqual((), manager.queued_ids())

    def test_second_submit_starts_immediately_at_concurrency_two(self):
        bodies = []
        manager = JobManager(max_concurrency=2, start_worker=bodies.append)
        manager.submit("a", lambda _handle: None)
        manager.submit("b", lambda _handle: None)

        self.assertEqual(2, len(bodies))
        self.assertEqual((), manager.queued_ids())


class JobManagerEscalationTests(unittest.TestCase):
    def _escalate(self, process, grace_seconds=5.0, kill_seconds=1.0):
        from SeparationWorker.job_manager import _escalate

        _escalate(process, grace_seconds, kill_seconds)

    def test_terminate_then_exit_inside_grace_never_calls_kill(self):
        process = FakeProcess(exits_after="terminate")

        self._escalate(process)

        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(0, process.kill_calls)

    def test_no_exit_within_grace_escalates_to_kill_exactly_once(self):
        process = FakeProcess(exits_after="kill")

        self._escalate(process)

        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)

    def test_cancel_on_a_running_job_escalates_its_registered_process(self):
        bodies = []
        manager = JobManager(start_worker=bodies.append, grace_seconds=0.0, kill_seconds=0.0)
        released = threading.Event()

        def run(handle):
            process = FakeProcess(exits_after=None)
            handle.register(process)
            released.wait(timeout=5)
            handle.unregister(process)
            self.last_process = process

        manager.submit("job-1", run)
        thread = threading.Thread(target=bodies[0])
        thread.start()
        # Give the worker a moment to register its process before cancelling.
        time.sleep(0.05)

        outcome = manager.cancel("job-1")
        released.set()
        thread.join(timeout=5)

        self.assertEqual("running", outcome)
        self.assertEqual(1, self.last_process.terminate_calls)
        self.assertEqual(1, self.last_process.kill_calls)

    def test_cancel_on_a_queued_job_removes_it_without_starting_it(self):
        bodies = []
        queue_snapshots = []
        manager = JobManager(start_worker=bodies.append, on_queue_change=queue_snapshots.append)
        started = []
        manager.submit("running", lambda _h: started.append("running"))
        manager.submit("queued", lambda _h: started.append("queued"))

        outcome = manager.cancel("queued")

        self.assertEqual("queued", outcome)
        self.assertEqual((), manager.queued_ids())
        bodies[0]()  # drain the still-running job
        self.assertEqual(["running"], started)

    def test_cancel_on_an_unknown_job_is_a_no_op(self):
        manager = JobManager(start_worker=lambda _fn: None)

        self.assertIsNone(manager.cancel("nope"))


class JobManagerShutdownTests(unittest.TestCase):
    def test_shutdown_returns_within_the_deadline_when_a_job_body_never_finishes(self):
        manager = JobManager()
        entered = threading.Event()
        block_forever = threading.Event()

        def run(_handle):
            entered.set()
            block_forever.wait()  # deliberately never set: simulates a job
            # body that ignores cancellation entirely.

        manager.submit("stuck", run)
        self.assertTrue(entered.wait(timeout=5))

        started = time.monotonic()
        result = manager.shutdown(deadline=0.3)
        elapsed = time.monotonic() - started

        self.assertFalse(result)
        self.assertLess(elapsed, 2.0)

    def test_shutdown_drains_queued_jobs_without_starting_them(self):
        manager = JobManager()
        started = []
        entered = threading.Event()
        release = threading.Event()

        def running_job(_handle):
            started.append("running")
            entered.set()
            release.wait(timeout=5)

        def queued_job(_handle):
            started.append("queued")

        manager.submit("running", running_job)
        self.assertTrue(entered.wait(timeout=5))
        manager.submit("queued-1", queued_job)
        manager.submit("queued-2", queued_job)
        self.assertEqual(("queued-1", "queued-2"), manager.queued_ids())

        # Let the running job finish shortly after shutdown starts waiting on
        # it, so the bounded join succeeds well within the deadline.
        threading.Timer(0.1, release.set).start()
        result = manager.shutdown(deadline=2.0)

        self.assertTrue(result)
        self.assertEqual(["running"], started)
        self.assertEqual((), manager.queued_ids())


class RunOwnedTests(unittest.TestCase):
    def test_run_owned_with_no_current_job_behaves_like_subprocess_run(self):
        self.assertIsNone(current_job())

        result = run_owned(
            [sys.executable, "-c", "print('hello-from-run-owned')"],
            stdout=subprocess.PIPE,
            text=True,
        )

        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(0, result.returncode)
        self.assertIn("hello-from-run-owned", result.stdout)

    def test_run_owned_with_no_current_job_raises_called_process_error_on_check(self):
        with self.assertRaises(subprocess.CalledProcessError):
            run_owned(
                [sys.executable, "-c", "import sys; sys.exit(3)"],
                check=True,
            )

    def test_run_owned_under_a_job_raises_job_cancelled_when_token_set_before_launch(self):
        manager = JobManager(start_worker=lambda fn: fn())
        outcomes = []

        def run(handle):
            handle.token.cancel()
            try:
                run_owned([sys.executable, "-c", "print('should not run')"])
            except JobCancelled:
                outcomes.append("cancelled")
            except Exception as error:  # pragma: no cover - failure diagnostic
                outcomes.append(error)

        manager.submit("job-1", run)

        self.assertEqual(["cancelled"], outcomes)

    def test_run_owned_under_a_job_raises_job_cancelled_not_called_process_error_when_killed(self):
        manager = JobManager(start_worker=lambda fn: fn(), grace_seconds=5.0, kill_seconds=2.0)
        outcomes = []

        def cancel_soon():
            time.sleep(0.3)
            manager.cancel("job-1")

        def run(_handle):
            threading.Thread(target=cancel_soon, daemon=True).start()
            try:
                run_owned([sys.executable, "-c", "import time; time.sleep(10)"])
            except JobCancelled:
                outcomes.append(JobCancelled)
            except subprocess.CalledProcessError as error:  # pragma: no cover - failure diagnostic
                outcomes.append(error)

        manager.submit("job-1", run)

        self.assertEqual([JobCancelled], outcomes)


if __name__ == "__main__":
    unittest.main()
