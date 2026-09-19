# Proposal: Review Phase 2 — Job Lifecycle (ARC-03 + ARC-04)

## Intent

Phase 2 of `PROJECT_REVIEW_2026-08-30.md` (ARC-03, ARC-04). Today the app can neither bound nor stop the work it starts.

- **ARC-03**: `SplitLibraryController.add()`/`retry()` call `_start_thread` unconditionally (`history.py:507-508,581-603`) — one new daemon thread per call, no queue, no semaphore. Each thread reaches `run_demucs()` (`demucs_adapter.py:113-135`), a blocking `subprocess.run` whose handle nobody keeps. Five drops = five concurrent Demucs processes competing for one GPU. Nothing can stop them: there is no cancel UI, and `_close()` (`gui.py:1901-1914`) never touches the controller, so closing the window leaves orphan `StemslayerWorker.exe` children running.
- **Cancellation is dead capability, not a partial fix.** `separate_audio()` forwards a `cancellation` token to `publish_atomic()`, but no production caller ever constructs a `CancellationToken` — `_prepare()` passes none. The mechanism in `engine/publication.py:109-147` is unreachable today.
- **ARC-04**: `runtime/supervisor.py`'s `Supervisor` is well-tested and production-uncalled. Its only `Process` implementation is `ScriptedProcess`, a test fake. It ships as a "security boundary that exists only in tests".

## Scope

### In Scope
- A `JobManager` owning job admission: FIFO queue, configurable concurrency, **default 1**.
- Real process ownership: retain the `Popen` handle per job; cancel/close escalates `.terminate()` → 5s grace → `.kill()`.
- The same ownership for `guitar_adapter.run_specialist` — it is a sub-step of a split job, not a separate action.
- End-to-end `CancellationToken` wiring so `publish_atomic`'s existing checks actually fire.
- A cancel action in the split library UI, and a drain-and-cancel step in `StemslayerApp._close()` that completes before the window is destroyed.
- Removal of `runtime/__init__.py`, `runtime/supervisor.py`, `runtime/worker_main.py`, `Tests/Portable/test_worker_runtime.py`.
- Regression tests for queueing, cancel, and close-drain in `Tests/Portable/`.

### Out of Scope
- Wiring `Supervisor` into production (needs a new production `Process` class, a `demucs_worker.py` protocol rewrite, and stdout/stderr separation — a materially larger change; see Approach).
- `protocol/framing.py`, `protocol/schemas.py`, `protocol/selftest.py` — confirmed live by the parent review.
- Any `SCHEMA_VERSION` bump or new `status` value.
- Progress/heartbeat reporting, hang detection, and worker sandboxing (`prepare_job_environment`) — later phases.
- Remaining review findings (PERF-*, QLT-*, UX-*, SEC-02/03, DAT-01, DEP-01).

## Capabilities

### New Capabilities
- `job-lifecycle`: bounded admission, owned child processes, and terminal cancellation for separation jobs.

### Modified Capabilities
- None (`openspec/specs/` is empty).

## Approach

Exploration Approach 1: a lightweight `Popen`-owned `JobManager`, reusing only the **shape** of `Supervisor.run`'s terminate→grace→kill escalation (`supervisor.py:220-230`), not its protocol-framing machinery.

Approach 2 ("wire in `Supervisor`") is rejected on evidence, not preference: no production `Process` implementation exists anywhere; `demucs_worker.py` is 13 lines that forward argv into `demucs.separate.main()` and speak no frames; `run_demucs` sets `stderr=STDOUT`, which corrupts a framed stream by construction; and `worker_main.py`'s in-process `install_network_denial()` assumes a worker topology this app does not ship. That is a new IPC layer, not a wiring change. If a sandboxed multi-process worker becomes a real product goal, it should be its own change — this proposal does not foreclose it, it just refuses to pay for it as a side effect.

## Resolved Product Decisions

Execution mode is `auto`; these were settled here rather than escalated. All four are engineering judgment inside the review's stated intent, not business-scope forks.

1. **Concurrency default → visible FIFO queue, concurrency 1. Add is never rejected.** Rejecting a second Add would be a capability regression versus today and loses work the user explicitly asked for; the drag-in-five-songs case is the normal case. "Bounded" is read as *bounded concurrent execution* (1 running job, 1 child process), not a capped queue: pending entries are memory-only and cost nothing. Queue state stays in memory and surfaces as a derived "Queued" label over the existing `preparing` row, because `tracks.status` is a SQLite `CHECK(status IN (...))` constraint (`history.py:153-155`) and adding `'queued'` would force a full table rebuild of the user's durable library — disproportionate to a lifecycle change. The ARC-02 input copy is staged when a job **starts**, not when it is enqueued, so a waiting queue costs no disk and ARC-02's hash-equals-separated-bytes invariant is unchanged.
2. **Cancel means kill. There is no graceful stop.** Demucs offers no clean interrupt point mid-`apply_model`, and Windows has no `SIGTERM` to catch. Cancel terminates the child; partial GPU/CPU work is lost and is not resumable. The UI must say so before confirming. Because the child gets no cleanup turn, the parent owns cleanup: discard the staged input copy and any partial result directory. The cancelled row lands on the existing terminal status `interrupted` with a distinct `error_detail`, which keeps `remove()` and `retry()` working and needs no schema change.
3. **`guitar_adapter.run_specialist` moves in this change.** Not for symmetry — for correctness. It is invoked from the split pipeline, so a specialist subprocess is part of a job the user can cancel and part of what must not survive `_close()`. Leaving it out would make this change's headline guarantee ("no orphan children on close") provably false.
4. **`runtime/` removal ships first, as its own slice.** It is pure deletion with zero coupling to ARC-03, it is the most trivially revertible unit in the change, and it likely lands near the 800-line review budget on deletions alone — bundling it would bury the `JobManager` diff underneath it. Same sequencing rationale Phase 1 used for ARC-01→ARC-02→SEC-01.

## Constraints

- **Must not regress `Tests/Portable`** — 411/411 passing on `fix/review-phase1-correctness-security` is the floor.
- **Windows-only process semantics.** No `SIGTERM`; `Popen.terminate()` is `TerminateProcess`. Escalation and any process-group handling must follow the platform-split pattern Phase 1 established for `msvcrt`/`fcntl` — never assume POSIX signals.
- No new runtime dependency (`Tools/requirements-portable.txt` stays stdlib-only).
- No edits to the vendored `demucs` package.
- No `SCHEMA_VERSION` / `PRAGMA user_version` change.
- Argv-list subprocess calls, no `shell=True`; preserve `CREATE_NO_WINDOW` and the frozen `frozen_worker_path()` resolution.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| new `JobManager` module | New | Queue, concurrency limit, process registry, terminate→kill escalation |
| `SeparationWorker/history.py` | Modified | `add`/`retry`/`_start_thread` (507-508, 581-603) route through the manager; `_prepare` (605-675) carries a token |
| `SeparationWorker/demucs_adapter.py` | Modified | `run_demucs` (113-135) becomes `Popen`-owned; `separate_audio` (438-575) propagates the token |
| `SeparationWorker/guitar_adapter.py` | Modified | `run_specialist` gains the same ownership and token |
| `SeparationWorker/gui.py` | Modified | Cancel action; `_close` (1901-1914) drains and waits |
| `SeparationWorker/runtime/` | Removed | All three files |
| `Tests/Portable/test_worker_runtime.py` | Removed | Only exercises removed code |
| `Tests/Portable/` | New | Queue, cancel, close-drain regressions |

## Delivery Slices

Independently revertible, chained in order:

1. **ARC-04** — delete `runtime/` + its test. Pure deletion.
2. **ARC-03 core** — `JobManager`, `Popen` ownership in both adapters, concurrency-1 queue wired into `history.py`.
3. **ARC-03 surface** — cancel UI, `_close()` drain, end-to-end token wiring.

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| A production import of `SeparationWorker.runtime` exists that the exploration's grep missed | Low | Slice 1 must prove zero non-test importers before deletion; the codegraph blast radius shows an ambiguous `run` edge from both adapters that is almost certainly a name collision, but it must be confirmed, not assumed |
| Killed child leaves a partial result directory or staged input behind | Med | Parent-side cleanup on the cancel path, reusing `discard_input_copy` / `purge_input_copies` (`history.py:457-494`) |
| `.terminate()` returns but the process lingers, hanging app close | Med | Bounded escalation (5s grace → `.kill()`), and close waits on a bounded deadline rather than an unbounded join |
| Derived "Queued" label diverges from DB truth after a crash | Low | Queue is memory-only; on restart `recover_unfinished()` already retires `preparing` rows to `interrupted` |
| Frozen-build process I/O behaves differently under PyInstaller | Med | Apply-time verification of `frozen_worker_path()` + `CREATE_NO_WINDOW` + pipe behavior, as Phase 1's C.1/C.2 did for `--repo` |
| Cancel racing publication commits a half-finished result | Low | `CancellationToken.commit_if_active` (`publication.py:122-126`) already serializes this — the fix is to make it reachable |

## Rollback Plan

Portable EXE ships via release tags, so rollback means republishing a prior tag; each slice must revert alone.

- **Slice 1**: restore the deleted files. Nothing referenced them, so nothing else changes.
- **Slice 2**: revert to `_start_thread` and blocking `subprocess.run`. No persisted state; queueing was memory-only.
- **Slice 3**: revert the cancel UI and `_close` drain. Rows previously cancelled remain `interrupted` — a status an older build already understands and can retry.

## Dependencies

None. No external prerequisite blocks this change.

## Success Criteria

- [ ] At most one Demucs child process runs at a time by default; additional Adds queue and start in order.
- [ ] Cancel terminates the running child within the escalation deadline and leaves no partial result directory or staged input.
- [ ] Closing the app leaves no surviving `StemslayerWorker.exe` or specialist child process.
- [ ] A `CancellationToken` set during publication reaches `publish_atomic` and blocks the commit.
- [ ] No production import of `SeparationWorker.runtime` remains.
- [ ] New `Tests/Portable/` tests fail against current `master` and pass after the change.
- [ ] `python -m unittest discover -s Tests\Portable -v` passes with no regression against the 411-test baseline.
