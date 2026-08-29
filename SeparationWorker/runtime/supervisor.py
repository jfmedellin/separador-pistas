import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from SeparationWorker.protocol.errors import ProtocolError
from SeparationWorker.protocol.schemas import validate_message


NETWORK_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "FTP_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "ftp_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
TERMINALS = frozenset({"completed", "failed", "cancelled"})


@dataclass(eq=False)
class WorkerFailure(RuntimeError):
    code: str
    cause: str
    recovery: str
    retryable: bool = True

    def __str__(self):
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


@dataclass(frozen=True)
class WorkerResult:
    kind: str
    message: dict


class _SystemClock:
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


def _failure(code, cause, recovery, retryable=True):
    return WorkerFailure(code, cause, recovery, retryable)


def _owned_directory(workspace, name):
    path = workspace / name
    path.mkdir(parents=True, exist_ok=False)
    return path


def prepare_job_environment(workspace, inherited=None):
    """Build a network-neutral environment whose mutable paths are job-local."""
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError("The job workspace must already exist.")
    environment = dict(os.environ if inherited is None else inherited)
    for key in NETWORK_ENVIRONMENT_KEYS:
        environment.pop(key, None)

    owned = []
    try:
        for name in ("home", "xdg-cache", "torch-cache", "temp", "bytecode-cache"):
            owned.append(_owned_directory(workspace, name))
    except Exception:
        cleanup_job_environment(owned)
        raise
    owned = tuple(owned)
    home, xdg_cache, torch_cache, temporary, bytecode = owned
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(xdg_cache),
            "TORCH_HOME": str(torch_cache),
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "TMPDIR": str(temporary),
            "PYTHONPYCACHEPREFIX": str(bytecode),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    return environment, owned


def cleanup_job_environment(owned_paths):
    for path in owned_paths:
        shutil.rmtree(path, ignore_errors=True)


class Supervisor:
    """Own worker timing, cancellation escalation, and terminal arbitration."""

    def __init__(
        self,
        expected_identities,
        *,
        clock=None,
        hello_timeout=30.0,
        silence_timeout=30.0,
        cancellation_grace=5.0,
        kill_grace=2.0,
        poll_interval=0.05,
    ):
        self.expected_identities = expected_identities
        self.clock = clock or _SystemClock()
        self.hello_timeout = hello_timeout
        self.silence_timeout = silence_timeout
        self.cancellation_grace = cancellation_grace
        self.kill_grace = kill_grace
        self.poll_interval = poll_interval

    def _validated_frame(self, process):
        frame = process.poll_frame()
        if frame is None:
            return None
        try:
            return validate_message(frame)
        except ProtocolError as error:
            raise _failure(
                "worker.protocol_error",
                str(error),
                "Use the matching protocol-v1 worker and inspect its control output.",
                False,
            ) from error

    def _check_hello(self, message):
        if message["type"] != "hello":
            raise _failure(
                "worker.hello_mismatch",
                "The first worker frame was not hello.",
                "Use a worker that begins with the expected protocol-v1 hello.",
                False,
            )
        if message.get("identities") != self.expected_identities:
            raise _failure(
                "worker.hello_mismatch",
                "Worker, runtime, or model identity did not match the approved job identity.",
                "Restore the approved local assets before starting another job.",
                False,
            )

    def _cancel_result(self, job):
        return WorkerResult("cancelled", {"type": "cancelled", "job": job})

    def run_job(self, process_factory, workspace, job, *, inherited=None, cancelled=lambda: False, publish=None):
        environment, owned_paths = prepare_job_environment(workspace, inherited)
        try:
            process = process_factory(environment)
            return self.run(process, job, cancelled=cancelled, publish=publish)
        finally:
            cleanup_job_environment(owned_paths)

    def run(self, process, job, *, cancelled=lambda: False, publish=None):
        started = self.clock.now()
        last_frame = started
        hello_seen = False
        cancel_started = None
        terminate_started = None
        terminal = None

        while True:
            now = self.clock.now()
            cancellation_requested = bool(cancelled())
            if cancellation_requested and cancel_started is None:
                cancel_started = now
                process.send({"type": "cancel", "job": job})

            message = self._validated_frame(process)
            if message is not None:
                last_frame = now
                if not hello_seen:
                    self._check_hello(message)
                    hello_seen = True
                elif message["type"] == "hello":
                    raise _failure(
                        "worker.protocol_error",
                        "The worker sent hello more than once.",
                        "Use one hello at process startup.",
                        False,
                    )
                elif message.get("job") != job:
                    raise _failure(
                        "worker.protocol_error",
                        "A worker frame referenced a different job.",
                        "Use one isolated worker process per job.",
                        False,
                    )
                elif message["type"] in TERMINALS:
                    if terminal is not None:
                        raise _failure(
                            "worker.terminal_cardinality",
                            "The worker sent duplicate or late terminal frames.",
                            "Discard all staged output and fix the worker to emit exactly one terminal.",
                            False,
                        )
                    terminal = message
                elif terminal is not None:
                    raise _failure(
                        "worker.protocol_error",
                        "The worker sent a frame after its terminal frame.",
                        "End worker output immediately after exactly one terminal frame.",
                        False,
                    )

            if message is not None and not process.is_alive():
                continue

            now = self.clock.now()
            if cancel_started is not None:
                if not process.is_alive():
                    return self._cancel_result(job)
                if terminate_started is None and now - cancel_started >= self.cancellation_grace:
                    process.terminate()
                    terminate_started = now
                    if not process.is_alive():
                        return self._cancel_result(job)
                elif terminate_started is not None and now - terminate_started >= self.kill_grace:
                    process.kill()
                    return self._cancel_result(job)
            elif not process.is_alive():
                if terminal is None:
                    raise _failure(
                        "worker.crashed",
                        f"The worker exited unexpectedly with code {process.exit_code!r}.",
                        "Inspect worker diagnostics and restart the complete job.",
                    )
                if terminal["type"] == "completed":
                    if publish is not None:
                        publish(terminal["manifest"])
                    return WorkerResult("completed", terminal)
                return WorkerResult(terminal["type"], terminal)
            elif not hello_seen and now - started >= self.hello_timeout:
                process.terminate()
                raise _failure(
                    "worker.hello_timeout",
                    "The worker did not send hello within 30 seconds.",
                    "Check the local runtime and restart the complete job.",
                )
            elif hello_seen and terminal is None and now - last_frame >= self.silence_timeout:
                process.terminate()
                raise _failure(
                    "worker.hung",
                    "The worker sent no valid heartbeat or progress frame for 30 seconds.",
                    "Inspect worker diagnostics and restart the complete job.",
                )

            self.clock.sleep(self.poll_interval)
