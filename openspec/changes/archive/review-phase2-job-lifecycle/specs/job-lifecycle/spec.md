# Job Lifecycle Specification

## Purpose

Separation jobs MUST be bounded, owned, and stoppable. Closes ARC-03 (unbounded concurrent Demucs subprocesses, no cancel path) and ARC-04 (untested, production-uncalled `Supervisor`). A `JobManager` owns admission and the `Popen` handle for every job-owned subprocess, including the `guitar_adapter` specialist sub-step, so cancellation and app close always reach the real child process.

## ADDED Requirements

### Requirement: Bounded Concurrent Admission With FIFO Queue

The system MUST run at most `max_concurrency` (default 1) job-owned subprocesses at a time. `add()`/`retry()` MUST NOT be rejected once that limit is reached; excess entries queue in memory-only FIFO order and start automatically as capacity frees.

#### Scenario: Fifth add during full capacity queues instead of failing

- GIVEN one job already running at default concurrency 1
- WHEN a user calls `add()` for four more sources
- THEN all four calls succeed, never rejected, and queue in FIFO order, starting one at a time as capacity frees

#### Scenario: Queued state is derived, not a new persisted status

- GIVEN a queued entry behind a running job
- WHEN its row is displayed
- THEN it shows a derived "Queued" label over the existing `preparing` status, with no new `tracks.status` value or `CHECK`-constraint change

### Requirement: Cancellation Terminates The Owned Subprocess

Cancelling a job MUST escalate against its owned `Popen` handle: `.terminate()` first, then `.kill()` if the process has not exited within a 5-second grace period.

#### Scenario: Cancel exits within the grace period

- GIVEN a running job whose subprocess responds to `.terminate()`
- WHEN the job is cancelled
- THEN the process exits before the grace period elapses and `.kill()` is never called

#### Scenario: Cancel escalates to kill after the grace period

- GIVEN a running job whose subprocess does not exit after `.terminate()`
- WHEN 5 seconds elapse without exit
- THEN the system calls `.kill()` on the same owned handle

### Requirement: Cancellation Leaves No Partial Artifacts And Lands On `interrupted`

A cancelled job MUST NOT leave a partial result directory or staged input copy on disk. Its row MUST land on the existing `interrupted` status with an `error_detail` distinct from other `interrupted` causes; no schema or `CHECK`-constraint change is introduced.

#### Scenario: Cancel discards staged input and partial output

- GIVEN a cancelled job with a staged input copy and a partial result directory
- WHEN cancellation completes
- THEN both are removed via the existing removal path

#### Scenario: Cancelled row keeps existing terminal status

- GIVEN a job cancelled mid-run
- WHEN cancellation completes
- THEN the row's status is `interrupted` with an `error_detail` distinctly identifying cancellation, and `retry()` on that row still works

### Requirement: App Close Drains And Cancels All Jobs Before Window Destruction

`StemslayerApp._close()` MUST cancel the running job and drain (cancel) every queued job, completing before the window is destroyed. No child process (`StemslayerWorker.exe` or specialist) may survive close.

#### Scenario: Close cancels one running and several queued jobs

- GIVEN one running job and three queued jobs
- WHEN `_close()` is invoked
- THEN the running job is cancelled via terminate→grace→kill, all three queued jobs drain without ever starting a subprocess, and the window is destroyed only after draining completes

#### Scenario: No child process survives close

- GIVEN a running Demucs job or specialist sub-step
- WHEN the application closes
- THEN no `StemslayerWorker.exe` process and no specialist process remain running

### Requirement: `CancellationToken` Reaches `publish_atomic`

A `CancellationToken` MUST be constructed per job and set when that job is cancelled; `publish_atomic`'s existing `commit_if_active` check MUST observe this state and block commit of a cancelled job's result.

#### Scenario: Cancel racing publication blocks the commit

- GIVEN a job whose separation finished and is about to call `publish_atomic`
- WHEN cancellation sets the token before `commit_if_active` runs
- THEN `publish_atomic` does not commit the result

#### Scenario: Non-cancelled job publishes normally

- GIVEN a job that completes without cancellation
- WHEN `publish_atomic` runs `commit_if_active`
- THEN the token remains inactive and the result commits as today

### Requirement: `guitar_adapter.run_specialist` Has The Same Ownership Guarantees As `demucs_adapter`

`run_specialist` MUST retain the `Popen` handle for its subprocess, honor the job's `CancellationToken` and terminate→grace→kill escalation, and be included in the close-drain path, identically to the Demucs adapter path.

#### Scenario: Cancel during the specialist sub-step terminates it

- GIVEN a job running its `guitar_adapter.run_specialist` sub-step
- WHEN the job is cancelled
- THEN the specialist subprocess is terminated via the same escalation as a Demucs job

#### Scenario: Close during the specialist sub-step leaves no survivor

- GIVEN a job running its specialist sub-step
- WHEN the application closes
- THEN the specialist process does not survive

### Requirement: No Production Import Of `SeparationWorker.runtime` Remains

After this change, no production code path MUST import `SeparationWorker.runtime`; `runtime/__init__.py`, `runtime/supervisor.py`, `runtime/worker_main.py`, and `Tests/Portable/test_worker_runtime.py` are removed.

#### Scenario: Static scan finds zero production importers

- GIVEN the codebase after this change
- WHEN production modules are scanned for imports of `SeparationWorker.runtime`
- THEN zero are found

#### Scenario: Removed test leaves no remaining reference

- GIVEN `Tests/Portable/test_worker_runtime.py` has been deleted
- WHEN the test suite runs
- THEN no other test references the deleted `runtime/` module, and the full `Tests/Portable` suite passes with no regression against the prior baseline
