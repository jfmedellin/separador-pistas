"""Admission, process ownership, and cancellation for job-owned subprocesses.

Owns three things that were previously unowned (ARC-03): **admission** (a
FIFO queue bounded to `max_concurrency` concurrent jobs), **the child
process** (a registered `Popen` per job, plus a best-effort Win32 job object
so a Demucs `--jobs N>0` process pool dies as a tree), and **the
cancellation token** (one `engine.publication.CancellationToken` per job,
finally reaching `publish_atomic`).

`run_owned()` is the one function every job-owned subprocess launch funnels
through -- both `demucs_adapter.run_demucs` and `guitar_adapter.run_specialist`
call it instead of `subprocess.run`. With no current job bound (CLI, `Tools/`,
plain unit tests) it behaves exactly like `subprocess.run`.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Sequence

from SeparationWorker.engine.publication import CancellationToken


class JobCancelled(RuntimeError):
    """Raised by run_owned when the owning job's token is set.

    Deliberately NOT a subprocess.CalledProcessError: `separate_audio` catches
    CalledProcessError on the CUDA attempt and reruns the whole inference on
    CPU. A killed child returns non-zero, so letting that surface as
    CalledProcessError would relaunch a multi-minute job the user just
    cancelled. JobCancelled propagates past that handler untouched.
    """


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="stemslayer-job", daemon=True).start()


@dataclass(eq=False)
class JobHandle:
    """One job's identity, cancellation token, and registered process set."""

    job_id: str
    token: CancellationToken
    _processes: list = field(default_factory=list, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def register(self, process) -> None:
        with self._lock:
            self._processes.append(process)

    def unregister(self, process) -> None:
        with self._lock:
            try:
                self._processes.remove(process)
            except ValueError:
                pass

    def _registered(self) -> list:
        with self._lock:
            return list(self._processes)


_local = threading.local()


def current_job() -> JobHandle | None:
    """Return the JobHandle bound to this thread by JobManager, if any."""
    return getattr(_local, "handle", None)


# --- Win32 job-object containment (D9) --------------------------------------
#
# subprocess.Popen.terminate()/kill() is TerminateProcess and reaches only the
# direct child. Demucs with --jobs N>0 spawns a process pool that, under
# PyInstaller, re-launches the same exe as its own children. A Win32 job
# object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE also reaps that tree if the
# job object's last handle closes -- including when the GUI process itself
# dies. Every ctypes call below is guarded: any failure degrades silently to
# plain per-process terminate/kill, never worse than before this change.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


def _win32_structures():
    import ctypes
    import ctypes.wintypes as wintypes

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return ctypes, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION


def _create_and_assign_job_object(pid: int):
    """Best-effort: create a job object with KILL_ON_JOB_CLOSE and assign `pid`.

    Returns an opaque handle on success, or None on any failure or on a
    non-Windows platform. Never raises.
    """
    if sys.platform != "win32":
        return None
    try:
        ctypes, extended_limits_type = _win32_structures()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = extended_limits_type()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        process_handle = kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid
        )
        if not process_handle:
            kernel32.CloseHandle(job)
            return None
        try:
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(process_handle)
        return job
    except Exception:
        return None


def _close_job_object(job) -> None:
    if job is None:
        return
    try:
        ctypes, _ = _win32_structures()
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
    except Exception:
        pass


def _terminate_job_object(job) -> bool:
    """Best-effort tree-kill via the job object. Returns False on any failure."""
    if job is None:
        return False
    try:
        ctypes, _ = _win32_structures()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(kernel32.TerminateJobObject(job, 1))
    except Exception:
        return False


def _own_process_tree(process) -> None:
    """Attach a best-effort Win32 job object to `process` (D9). Never raises."""
    try:
        process._stemslayer_job = _create_and_assign_job_object(process.pid)
    except Exception:
        process._stemslayer_job = None


def _release_process_tree(process) -> None:
    job = getattr(process, "_stemslayer_job", None)
    _close_job_object(job)
    try:
        process._stemslayer_job = None
    except Exception:
        pass


# --- Escalation (D9) ---------------------------------------------------------


def _escalate(process, grace_seconds: float, kill_seconds: float) -> None:
    """`.terminate()`, then `.kill()` (tree-kill if a job object is attached)
    if the process has not exited within `grace_seconds`. Never raises."""
    try:
        process.terminate()
    except Exception:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    job = getattr(process, "_stemslayer_job", None)
    killed_tree = _terminate_job_object(job) if job is not None else False
    if not killed_tree:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=kill_seconds)
    except Exception:
        pass


# --- run_owned (D5, D6, D7, D8) ----------------------------------------------


def run_owned(command: Sequence[str], **options):
    """subprocess.run's contract, plus ownership.

    Raises JobCancelled if the current job's token is set at entry or at
    exit; otherwise raises the identical subprocess.CalledProcessError that
    check=True would produce, so _diagnostic_tail/_failure_cause keep working
    unchanged. Returns only after the owned process tree has exited.

    With no current job bound (D5), this is exactly subprocess.run.
    """
    handle = current_job()
    if handle is None:
        return subprocess.run(command, **options)

    token = handle.token
    if token.cancelled:
        raise JobCancelled(f"Job {handle.job_id!r} was cancelled before its subprocess started.")

    check = bool(options.pop("check", False))
    process = subprocess.Popen(list(command), **options)
    _own_process_tree(process)
    handle.register(process)
    try:
        stdout, stderr = process.communicate()
    finally:
        handle.unregister(process)
        _release_process_tree(process)

    if token.cancelled:
        raise JobCancelled(f"Job {handle.job_id!r} was cancelled.")
    if check and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


# --- JobManager (D2, D3, D4, D10) --------------------------------------------


class JobManager:
    """FIFO admission bounded to `max_concurrency`, owning cancellation and
    bounded shutdown for every job it starts."""

    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        start_worker: Callable[[Callable[[], None]], None] = _start_thread,
        on_queue_change: Callable[[tuple[str, ...]], None] = lambda _ids: None,
        grace_seconds: float = 5.0,
        kill_seconds: float = 1.0,
    ) -> None:
        self._max_concurrency = max_concurrency
        self._start_worker = start_worker
        self._on_queue_change = on_queue_change
        self._grace_seconds = grace_seconds
        self._kill_seconds = kill_seconds
        self._lock = threading.Lock()
        self._pending: deque = deque()
        self._active: dict[str, JobHandle] = {}
        self._finished: dict[str, threading.Event] = {}
        self._draining = False
        self._pumping = False

    def submit(self, job_id: str, run: Callable[[JobHandle], None]) -> None:
        with self._lock:
            self._pending.append((job_id, run))
            queued_ids = tuple(pending_job_id for pending_job_id, _ in self._pending)
        # Report the new pending entry immediately (D4): _pump() below only
        # fires on_queue_change when it actually promotes or drains a job, so
        # without this, a job submitted behind a full concurrency slot would
        # report no queue position at all until some other job's start/finish
        # happened to move the queue -- silently hiding the very first queued
        # entry's "Queued" state for its entire wait, not just delaying it.
        self._on_queue_change(queued_ids)
        self._pump()

    def cancel(self, job_id: str) -> str | None:
        with self._lock:
            for index, (pending_id, _run) in enumerate(self._pending):
                if pending_id == job_id:
                    del self._pending[index]
                    queued_ids = tuple(pending_job_id for pending_job_id, _ in self._pending)
                    self._on_queue_change(queued_ids)
                    return "queued"
            handle = self._active.get(job_id)
        if handle is None:
            return None
        handle.token.cancel()
        for process in handle._registered():
            _escalate(process, self._grace_seconds, self._kill_seconds)
        return "running"

    def queued_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(job_id for job_id, _ in self._pending)

    def shutdown(self, *, deadline: float = 8.0) -> bool:
        import time

        started = time.monotonic()
        with self._lock:
            self._draining = True
            self._pending.clear()
            running = list(self._active.items())
            events = dict(self._finished)
        if running:
            self._on_queue_change(())
        for _job_id, handle in running:
            handle.token.cancel()
            for process in handle._registered():
                remaining = max(0.0, deadline - (time.monotonic() - started))
                _escalate(
                    process,
                    min(self._grace_seconds, remaining),
                    min(self._kill_seconds, remaining),
                )
        all_joined = True
        for _job_id, event in events.items():
            remaining = max(0.0, deadline - (time.monotonic() - started))
            if not event.wait(timeout=remaining):
                all_joined = False
        return all_joined

    def _make_body(self, job_id, run, handle, finished_event):
        def body() -> None:
            _local.handle = handle
            try:
                run(handle)
            finally:
                try:
                    del _local.handle
                except AttributeError:
                    pass
                with self._lock:
                    self._active.pop(job_id, None)
                    self._finished.pop(job_id, None)
                finished_event.set()
                self._pump()

        return body

    def _pump(self) -> None:
        with self._lock:
            if self._pumping:
                return
            self._pumping = True
        try:
            while True:
                with self._lock:
                    if (
                        self._draining
                        or len(self._active) >= self._max_concurrency
                        or not self._pending
                    ):
                        break
                    job_id, run = self._pending.popleft()
                    handle = JobHandle(job_id, CancellationToken())
                    finished_event = threading.Event()
                    self._active[job_id] = handle
                    self._finished[job_id] = finished_event
                    queued_ids = tuple(pending_job_id for pending_job_id, _ in self._pending)
                self._on_queue_change(queued_ids)
                self._start_worker(self._make_body(job_id, run, handle, finished_event))
        finally:
            with self._lock:
                self._pumping = False
        with self._lock:
            resume = (
                not self._draining
                and bool(self._pending)
                and len(self._active) < self._max_concurrency
            )
        if resume:
            self._pump()
