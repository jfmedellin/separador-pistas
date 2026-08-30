import os
import socket
import tempfile
import unittest
from pathlib import Path

from SeparationWorker.protocol.errors import ProtocolError
from SeparationWorker.protocol.schemas import validate_message
from SeparationWorker.runtime.supervisor import (
    Supervisor,
    WorkerFailure,
    cleanup_job_environment,
    prepare_job_environment,
)
from SeparationWorker.runtime.worker_main import install_network_denial, runtime_messages


IDENTITIES = {
    "worker": {"id": "stemslayer-worker", "sha256": "a" * 64},
    "runtime": {"id": "cpython-portable", "sha256": "b" * 64},
    "model": {"id": "test-model", "sha256": "c" * 64},
}


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ScriptedProcess:
    def __init__(self, clock, events=(), exit_at=None, ignore_terminate=False):
        self.clock = clock
        self.events = list(events)
        self.exit_at = exit_at
        self.ignore_terminate = ignore_terminate
        self.sent = []
        self.terminated = False
        self.killed = False

    def poll_frame(self):
        if self.events and self.events[0][0] <= self.clock.now():
            return self.events.pop(0)[1]
        return None

    def send(self, message):
        self.sent.append(message)

    def is_alive(self):
        if self.killed or (self.terminated and not self.ignore_terminate):
            return False
        return self.exit_at is None or self.clock.now() < self.exit_at

    @property
    def exit_code(self):
        return None if self.is_alive() else 1

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def hello(identities=IDENTITIES):
    return {
        "type": "hello",
        "protocol": {"major": 1},
        "worker": "1.0.0",
        "runtime": "portable",
        "capabilities": ["vocals", "drums", "bass", "other"],
        "identities": identities,
    }


def terminal(kind="completed"):
    message = {"type": kind, "job": "job-1"}
    if kind == "completed":
        message["manifest"] = {"files": ["vocals.wav"]}
    if kind == "failed":
        message["error"] = {
            "code": "worker.inference_failed",
            "stage": "running",
            "cause": "Synthetic worker failure.",
            "recovery": "Inspect the job and retry.",
            "retryable": True,
        }
    return message


class WorkerProtocolTests(unittest.TestCase):
    def test_accepts_identity_hello_heartbeat_and_progress(self):
        self.assertEqual("hello", validate_message(hello())["type"])
        self.assertEqual(
            "heartbeat",
            validate_message({"type": "heartbeat", "job": "job-1", "stage": "running"})["type"],
        )
        self.assertEqual(
            "progress",
            validate_message(
                {"type": "progress", "job": "job-1", "stage": "running", "completed": 1, "total": 2}
            )["type"],
        )

    def test_worker_messages_never_exceed_five_second_cadence(self):
        messages = list(runtime_messages("job-1", IDENTITIES, total_steps=3, cadence_seconds=5.0))
        self.assertEqual(["hello", "progress", "heartbeat", "progress", "heartbeat", "progress", "completed"], [m[1]["type"] for m in messages])
        self.assertTrue(all(b[0] - a[0] <= 5.0 for a, b in zip(messages, messages[1:])))

    def test_network_is_denied_even_when_proxy_is_inherited(self):
        original_socket = socket.socket
        original_create = socket.create_connection
        try:
            install_network_denial()
            with self.assertRaises(PermissionError):
                socket.create_connection(("127.0.0.1", 9))
            with self.assertRaises(PermissionError):
                socket.socket()
        finally:
            socket.socket = original_socket
            socket.create_connection = original_create


class WorkerLifecycleTests(unittest.TestCase):
    def run_supervisor(self, events=(), **process_options):
        clock = FakeClock()
        process = ScriptedProcess(clock, events, **process_options)
        publications = []
        supervisor = Supervisor(IDENTITIES, clock=clock, poll_interval=1.0)
        try:
            result = supervisor.run(process, "job-1", publish=lambda manifest: publications.append(manifest))
            return result, process, publications, clock
        except WorkerFailure as error:
            error.process = process
            error.publications = publications
            error.clock = clock
            raise

    def assert_failure(self, code, events=(), **options):
        with self.assertRaises(WorkerFailure) as caught:
            self.run_supervisor(events, **options)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual([], caught.exception.publications)
        return caught.exception

    def test_hello_timeout_is_deterministic(self):
        failure = self.assert_failure("worker.hello_timeout")
        self.assertEqual(30.0, failure.clock.now())

    def test_hello_identity_mismatch_fails_closed(self):
        mismatched = dict(IDENTITIES)
        mismatched["model"] = {"id": "wrong", "sha256": "d" * 64}
        self.assert_failure("worker.hello_mismatch", [(0, hello(mismatched))])

    def test_unexpected_exit_is_a_crash(self):
        self.assert_failure("worker.crashed", exit_at=0)

    def test_thirty_seconds_without_a_frame_is_hung(self):
        failure = self.assert_failure("worker.hung", [(0, hello())])
        self.assertEqual(30.0, failure.clock.now())

    def test_heartbeat_and_progress_keep_worker_live_until_one_terminal(self):
        events = [(0, hello())]
        events += [(value, {"type": "heartbeat", "job": "job-1", "stage": "running"}) for value in (5, 10, 15, 20, 25, 30)]
        events += [(31, {"type": "progress", "job": "job-1", "stage": "running", "completed": 1, "total": 1}), (32, terminal())]
        result, _, publications, _ = self.run_supervisor(events, exit_at=33)
        self.assertEqual("completed", result.kind)
        self.assertEqual(1, len(publications))

    def test_zero_duplicate_and_late_terminals_never_publish(self):
        self.assert_failure("worker.crashed", [(0, hello())], exit_at=1)
        duplicate = [(0, hello()), (1, terminal()), (1, terminal())]
        self.assert_failure("worker.terminal_cardinality", duplicate, exit_at=2)
        late = [(0, hello()), (1, terminal()), (1, terminal("failed"))]
        self.assert_failure("worker.terminal_cardinality", late, exit_at=2)

    def test_immediate_exit_rejects_buffered_terminal_before_publication(self):
        for buffered in (terminal(), terminal("failed")):
            with self.subTest(buffered=buffered["type"]):
                events = [(0, hello()), (1, terminal()), (1, buffered)]
                self.assert_failure("worker.terminal_cardinality", events, exit_at=1)

    def test_cancel_waits_five_seconds_then_terminates(self):
        clock = FakeClock()
        process = ScriptedProcess(clock, [(0, hello())])
        supervisor = Supervisor(IDENTITIES, clock=clock, poll_interval=1.0)
        result = supervisor.run(process, "job-1", cancelled=lambda: clock.now() >= 1)
        self.assertEqual("cancelled", result.kind)
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(6.0, clock.now())

    def test_cancel_kills_two_seconds_after_ignored_terminate(self):
        clock = FakeClock()
        process = ScriptedProcess(clock, [(0, hello())], ignore_terminate=True)
        result = Supervisor(IDENTITIES, clock=clock, poll_interval=1.0).run(
            process, "job-1", cancelled=lambda: clock.now() >= 1
        )
        self.assertEqual("cancelled", result.kind)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(8.0, clock.now())

    def test_completion_racing_with_cancel_is_cancelled_and_not_published(self):
        clock = FakeClock()
        process = ScriptedProcess(clock, [(0, hello()), (1, terminal())], exit_at=2)
        publications = []
        result = Supervisor(IDENTITIES, clock=clock, poll_interval=1.0).run(
            process,
            "job-1",
            cancelled=lambda: clock.now() >= 1,
            publish=lambda manifest: publications.append(manifest),
        )
        self.assertEqual("cancelled", result.kind)
        self.assertEqual([], publications)

    def test_failed_terminal_does_not_publish(self):
        result, _, publications, _ = self.run_supervisor([(0, hello()), (1, terminal("failed"))], exit_at=2)
        self.assertEqual("failed", result.kind)
        self.assertEqual([], publications)


class WorkerIsolationTests(unittest.TestCase):
    def test_partial_environment_setup_cleans_only_new_owned_paths(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "job"
            workspace.mkdir()
            unrelated = workspace / "xdg-cache"
            unrelated.mkdir()
            marker = unrelated / "keep.txt"
            marker.write_text("unrelated", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                prepare_job_environment(workspace, {})

            self.assertFalse((workspace / "home").exists())
            self.assertEqual("unrelated", marker.read_text(encoding="utf-8"))

    def test_environment_is_job_local_and_cleanup_removes_it(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "job"
            workspace.mkdir()
            inherited = {"HTTP_PROXY": "http://proxy.invalid", "PATH": os.environ.get("PATH", "")}
            environment, owned = prepare_job_environment(workspace, inherited)
            for key in ("HOME", "XDG_CACHE_HOME", "TORCH_HOME", "TMP", "TEMP", "PYTHONPYCACHEPREFIX"):
                self.assertTrue(Path(environment[key]).resolve().is_relative_to(workspace.resolve()))
            self.assertEqual("1", environment["PYTHONDONTWRITEBYTECODE"])
            self.assertNotIn("HTTP_PROXY", environment)
            self.assertTrue(all(path.exists() for path in owned))
            cleanup_job_environment(owned)
            self.assertTrue(all(not path.exists() for path in owned))
            self.assertTrue(workspace.exists())

    def test_supervised_job_cleans_environment_after_failure(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "job"
            workspace.mkdir()
            clock = FakeClock()
            supervisor = Supervisor(IDENTITIES, clock=clock, poll_interval=1.0)
            captured = {}

            def process_factory(environment):
                captured.update(environment)
                return ScriptedProcess(clock, exit_at=0)

            with self.assertRaises(WorkerFailure):
                supervisor.run_job(process_factory, workspace, "job-1", inherited={})
            self.assertFalse(Path(captured["TORCH_HOME"]).exists())
            self.assertFalse(Path(captured["TMP"]).exists())
            self.assertTrue(workspace.exists())


if __name__ == "__main__":
    unittest.main()
