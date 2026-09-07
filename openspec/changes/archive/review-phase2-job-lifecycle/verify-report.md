```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: git:a26d47a5725f39f03341fcea2b46a849807045c3
verdict: pass
blockers: 0
critical_findings: 0
requirements: 7/7
scenarios: 14/14
test_command: .venv-portable/Scripts/python.exe -m unittest discover -s Tests/Portable -v
test_exit_code: 0
test_output_hash: sha256:d06fd02c1935ec0e4b3a9ed64d61519faea61b47832c10fb4306657a79a06a54
build_command: N/A (no compiled-build step for this change; portable PyInstaller build deferred to a human, per C.5/C.7/C.8)
build_exit_code: 0
build_output_hash: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

## Verification Report

**Change**: review-phase2-job-lifecycle
**Version**: N/A (single spec revision)
**Mode**: Standard
**Re-verification of**: prior FAIL verdict (1 CRITICAL, 4 WARNING) after remediation commit `1b64057` ("fix: report queued position immediately on submit, not only on promotion")

### Completeness
| Metric | Value |
|--------|-------|
| Tasks total | 29 |
| Tasks complete | 26 |
| Tasks incomplete | 3 (C.5, C.7, C.8, apply-time verification requiring a real portable/PyInstaller build; explicitly documented as deferred in tasks.md and state.yaml, unchanged since the prior pass) |

### Build & Tests Execution
**Build**: N/A, no build step for this change beyond the existing portable venv; PyInstaller build explicitly out of scope for dev-shell verification (C.5/C.7/C.8).

**Tests**: 417 passed / 0 failed / 0 skipped
```text
$ .venv-portable/Scripts/python.exe -m unittest discover -s Tests/Portable -v
...
Ran 417 tests in 30.094s

OK
```
Independently re-run in this verification pass. Up from 414/414 at the prior pass (+3: the new controller-level FIFO propagation test, the new GUI "Queued"-label render test, and the new end-to-end SplitLibraryController.cancel() test against a real killed subprocess), matching the delta claimed by commit 1b64057.

**Coverage**: Not measured (no coverage tool configured for this project).

### Commit 1b64057 Correctness Review

The commit is not test-only: it also changes JobManager.submit() in SeparationWorker/job_manager.py. Reviewed the actual behavior, not just the tests written to cover it.

1. The bug it fixes is real and correctly diagnosed. Before the fix, submit() only called self._pump(); on_queue_change fired only from inside _pump()'s promotion loop, i.e. only when a job was actually popped from _pending. At default concurrency 1, the first job submitted behind an already-running job would sit in _pending with no on_queue_change call ever naming it, until some other job's completion later drained the queue and happened to pop it, meaning it could report "processing" directly with no "Queued" state ever shown for its entire wait. The fix adds one call, self._on_queue_change(queued_ids), right after the with self._lock: self._pending.append(...) block in submit(), before the existing self._pump() call. This directly closes the gap.

2. Traced the immediate-promotion case by hand (single thread, no interleaving). submit("a", ...) when a concurrency slot is free: (a) lock, append "a", compute queued_ids = ("a",), unlock; (b) fire on_queue_change(("a",)); (c) call self._pump(), which, same thread, synchronously, before submit() returns, re-acquires the lock, finds a free slot, pops "a", computes queued_ids = (), unlocks, and fires on_queue_change(()). So by the time submit() returns, the last call the listener received for job "a" is the correct, non-queued state. Confirmed exactly against test_fifth_submit_at_default_concurrency_queues_and_starts_in_fifo_order's new expected sequence: ("a",), () as the first two entries. No stale final state in this path.

3. NEW finding, a genuine race the fix introduces (see Issues, WARNING #1). submit()'s immediate report is captured under the lock but delivered to on_queue_change outside the lock, with no ordering guarantee against a concurrently running _pump() triggered on a different thread (a job finishing calls _pump() from _make_body's finally). If that other thread's _pump() pops and reports the same newly submitted job as promoted before the submitting thread's own, now-stale, immediate report is delivered, the stale "queued" report lands last in the GUI's FIFO dispatch queue (queue.SimpleQueue, drained strictly in put() order, gui.py line 472/509+) and overwrites the correct state. The job would then be shown as "Queued" for the rest of its actual run, since it fires no further on_queue_change while active, only self-correcting when some other unrelated queue-changing event happens to fire again. This requires precise interleaving (one job's completion racing another's submission) and is not exercised by any test: the new tests all use explicit threading.Event gating specifically to serialize submissions after the previous job is confirmed started, which avoids this exact window by construction. Not data-corrupting (job execution, history persistence, and FIFO admission order are all unaffected, this is a derived, transient UI label only), but it is a real, previously absent correctness gap in the same feature the original CRITICAL finding was about. See Issues below.

4. The 3 new tests exercise the real path, not just internal state (verified individually, see Spec Compliance Matrix).

5. Hand traced test_fifth_submit_at_default_concurrency_queues_and_starts_in_fifo_order's updated queue_snapshots assertion against the actual code with start_worker=bodies.append (synchronous, single threaded test double, no race window). Full manual trace of all 5 submit() calls plus the 5 sequential drains reproduces exactly:
   [("a",), (), ("b",), ("b","c"), ("b","c","d"), ("b","c","d","e"), ("c","d","e"), ("d","e"), ("e",), ()]
   matching the test's hardcoded expectation element for element. The assertion is correct for the new code, not merely adjusted to match a bug.

### Spec Compliance Matrix
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Bounded Concurrent Admission With FIFO Queue | Fifth add during full capacity queues instead of failing | test_job_manager.py JobManagerQueueTests test_fifth_submit_at_default_concurrency_queues_and_starts_in_fifo_order | COMPLIANT |
| Bounded Concurrent Admission With FIFO Queue | Queued state is derived, not a new persisted status | test_job_manager.py (JobManager-level immediate report) plus test_history.py test_controller_propagates_queued_track_ids_in_fifo_order_as_the_running_job_drains (controller-level FIFO propagation, real threads) plus test_gui_library_rows.py QueuedLabelRenderTests test_queued_row_renders_the_queued_label_while_its_status_stays_preparing (real Tk app, real _render_library) | COMPLIANT, prior CRITICAL closed |
| Cancellation Terminates The Owned Subprocess | Cancel exits within the grace period | test_job_manager.py JobManagerEscalationTests test_terminate_then_exit_inside_grace_never_calls_kill | COMPLIANT |
| Cancellation Terminates The Owned Subprocess | Cancel escalates to kill after the grace period | test_job_manager.py JobManagerEscalationTests test_no_exit_within_grace_escalates_to_kill_exactly_once | COMPLIANT |
| Cancellation Leaves No Partial Artifacts And Lands On interrupted | Cancel discards staged input and partial output | test_history.py test_cancel_mid_run_lands_on_interrupted_with_no_partial_artifacts_and_retry_still_works | COMPLIANT |
| Cancellation Leaves No Partial Artifacts And Lands On interrupted | Cancelled row keeps existing terminal status | test_history.py test_cancel_mid_run_lands_on_interrupted_with_no_partial_artifacts_and_retry_still_works | COMPLIANT |
| App Close Drains And Cancels All Jobs Before Window Destruction | Close cancels one running and several queued jobs | test_history.py test_shutdown_cancels_the_running_job_and_drains_three_queued_jobs_within_the_deadline plus test_gui_library_rows.py CloseDrainTests | COMPLIANT |
| App Close Drains And Cancels All Jobs Before Window Destruction | No child process survives close | test_gui_library_rows.py CloseDrainTests test_close_kills_the_running_child_process_and_destroys_only_after_the_drain_completes, real subprocess.Popen registered on the job handle | COMPLIANT |
| CancellationToken Reaches publish_atomic | Cancel racing publication blocks the commit | test_history.py test_commit_if_active_blocks_the_commit_when_cancelled_just_before_publication | COMPLIANT |
| CancellationToken Reaches publish_atomic | Non-cancelled job publishes normally | existing broad regression suite, e.g. test_history.py test_adds_immediately_then_publishes_durable_ready_result, unchanged and still passing | COMPLIANT |
| guitar_adapter.run_specialist Ownership Parity | Cancel during the specialist sub-step terminates it | source-verified one-line run_owned swap at guitar_adapter.py line 178; underlying kill guarantee proven generically, not specialist-specific, by test_job_manager.py RunOwnedTests test_run_owned_under_a_job_raises_job_cancelled_not_called_process_error_when_killed, a real subprocess | PARTIAL, unchanged |
| guitar_adapter.run_specialist Ownership Parity | Close during the specialist sub-step leaves no survivor | same reasoning, JobManager.shutdown is adapter-agnostic and proven generically, not with a live specialist process | PARTIAL, unchanged |
| No Production Import Of SeparationWorker.runtime Remains | Static scan finds zero production importers | re-verified: rg repo-wide grep zero hits for Supervisor and supervisor in both adapters and for SeparationWorker.runtime imports | COMPLIANT |
| No Production Import Of SeparationWorker.runtime Remains | Removed test leaves no remaining reference | Tests/Portable/test_worker_runtime.py confirmed absent from disk, full suite 417/417 passes | COMPLIANT |

Compliance summary: 12/14 scenarios fully compliant (up from 11/14), 2/14 partial (unchanged, source-verified plus generically-tested only), 0/14 untested (down from 1/14, the prior CRITICAL gap).

### Correctness (Static Evidence)
| Requirement / Decision | Status | Notes |
|------------|--------|-------|
| D4, on_queue_change reflects FIFO position including at submit time | Implemented, with one caveat | job_manager.py submit() lines 309-320: reports immediately after enqueueing, in addition to _pump()'s existing promotion/drain reports; correct for the single-thread and no-interleaving case (traced by hand above); see NEW WARNING for a cross-thread race this introduces |
| D5-D9, ambient registry, explicit token, JobCancelled, dual entry and exit check, Win32 tree-kill | Implemented | Unchanged from prior pass; re-confirmed by full suite pass, no diff in this area since 1b64057 touches only submit() plus tests |
| D10-D13, bounded shutdown, one confirmation, reuse-only cleanup, guitar_adapter parity | Implemented | Unchanged from prior pass; 1b64057's diff --stat touches only job_manager.py (+8/-0) and the three test files, confirmed by git show 1b64057 --stat |
| Phase 1/Phase 2 prior regression check | No regression | 1b64057 is additive-only to job_manager.py (8 new lines, 0 removed); demucs_adapter.py, guitar_adapter.py, history.py's non-test code, and gui.py are untouched by this commit |

### Coherence (Design)
| Decision | Followed? | Notes |
|----------|-----------|-------|
| D1-D13 | Yes, with one new residual-risk note | See Correctness table and Issues below for the one new WARNING found in this pass |

### Issues Found

CRITICAL: None. The prior CRITICAL (the "Queued state is derived" scenario had zero test coverage above raw JobManager.on_queue_change) is closed: test_controller_propagates_queued_track_ids_in_fifo_order_as_the_running_job_drains exercises real SplitLibraryController FIFO propagation with real background threads, and test_queued_row_renders_the_queued_label_while_its_status_stays_preparing exercises the real gui.py _render_library path with a real Tk app, asserting the literal "Queued" label renders.

WARNING:
1. NEW: JobManager.submit()'s new immediate on_queue_change report is captured under the lock but delivered outside it, racing a concurrently running _pump() on a different thread (a job completing, via _make_body's finally). If that thread promotes the same just-submitted job and delivers its correct report first, the submitting thread's now-stale "still queued" report can be delivered second (the GUI dispatch queue, queue.SimpleQueue, has no sequencing to drop out-of-order updates) and permanently mis-display an actually-running job as "Queued" for the remainder of its run. Not data-corrupting (job execution, persistence, and admission order unaffected), narrow window, not exercised by any test (the new tests deliberately serialize submissions with events to avoid it). See Suggestion #1.
2. SplitLibraryController.cancel() itself is now covered end-to-end with a real killed subprocess (test_cancel_terminates_a_real_running_subprocess_end_to_end_and_lands_on_interrupted), closing the prior WARNING at the controller layer. However gui.py's _cancel_library_track (the actual per-row Cancel button handler, gated by messagebox.askyesno) is still never exercised by any GUI-level test; the new test calls controller.cancel(track_id) directly, bypassing the confirm dialog. This is a narrower version of the prior WARNING, not fully closed; the commit message's framing ("the exact GUI Cancel button's call path") is accurate for the cancel mechanics but not for the button-click and confirm-dialog wiring itself.
3. guitar_adapter.run_specialist's two D13 scenarios remain verified only by source-level identical-swap confirmation and adapter-agnostic run_owned and JobManager tests using a generic subprocess, not specialist-shaped. Unchanged from the prior pass; consistent with the design's explicit D13 scoping (zero production callers today).
4. Tasks C.5, C.7, C.8 remain unchecked, requiring a real portable PyInstaller build plus Task Manager observation. Unchanged from the prior pass, matches tasks.md's and state.yaml's own explicit documentation of this gap.

SUGGESTION:
1. Close WARNING #1: add a monotonic sequence number to on_queue_change payloads, or otherwise make the listener reject an update older than the last one it applied, so a stale report delivered after a fresher one cannot silently win.
2. Close WARNING #2: add one GUI-level test, analogous to CloseConfirmationDeclinedTests, that actually drives _cancel_library_track's confirm dialog and button, instead of calling controller.cancel() directly.
3. Add a specialist-shaped, not generic, subprocess test for guitar_adapter's D13 scenarios if and when it gains a production caller.

## Re-verification Pass 3: Commit a26d47a ("fix: close a cross-thread race in queue-position reporting")

**Re-verification of**: the single NEW WARNING raised by Pass 2 above -- the cross-thread race where `submit()`'s and `_pump()`'s `on_queue_change` calls both fired after releasing `JobManager._lock`, letting a stale "still queued" report land after a correct "now running" report and permanently mis-display an active job as queued.

### What changed (`git show a26d47a`, +22/-10, `SeparationWorker/job_manager.py` only)

Every `on_queue_change` call site is moved to fire while still holding `self._lock`, instead of after releasing it:
- `submit()`: the immediate report (added in commit `1b64057`, the fix Pass 2 reviewed) now sits inside the same `with self._lock:` block as the `_pending.append(...)` and `queued_ids` computation, rather than after it.
- `_pump()`'s promotion loop: `self._on_queue_change(queued_ids)` moved from just after the `with self._lock:` block to just before it closes, still inside the block that pops the job, creates its `JobHandle`, and updates `self._active`/`self._finished`.
- `shutdown()`: the `if running: self._on_queue_change(())` line moved one indent level inward, now inside the same `with self._lock:` block that sets `self._draining = True` and clears `self._pending`.
- `cancel()`'s queued-job branch already fired its report inside the lock before this commit; unchanged, now the pattern every other site follows.

No test files changed in this commit; the commit message's "417/417 ... unchanged" claim is confirmed structurally by `git show a26d47a --stat` showing only `job_manager.py` touched, and independently by re-running the suite in this pass (below).

### 1. Full suite re-run (this pass)

```
$ .venv-portable/Scripts/python.exe -m unittest discover -s Tests/Portable -v
...
Ran 417 tests in 29.998s

OK
```
Run twice in this pass (29.973s and 29.998s), both consistent with the ~30s baseline from Pass 2 (30.094s) and this repo's prior runs. 417 passed / 0 failed / 0 skipped, exit code 0. No hang, no new timeout, no slowdown that would indicate a locking regression (a deadlock would have hung the run indefinitely; a specific test with an explicit `timeout=`/`wait()` bound would have failed instead of passing quickly). Confirms the commit message's "unchanged" claim rather than accepting it on faith.

### 2. Deadlock safety of holding `self._lock` across the `on_queue_change` call

`threading.Lock` is non-reentrant: the same thread re-acquiring it while already holding it deadlocks immediately. The new code only stays safe if no code path reachable from `self._on_queue_change(...)` calls back into `JobManager.submit`/`cancel`/`shutdown`/`queued_ids`/`_pump` on the *same* thread while the lock is held.

- **Production wiring** (the only real caller): `JobManager(..., on_queue_change=self._queue_changed)` in `history.py`'s `SplitLibraryController.__init__`. `_queue_changed` (`history.py:552-560`) does exactly one thing: `self._dispatch(lambda: self._set_queued_ids(queued_ids))`. In production (`gui.py:525`), `dispatch=self._events.put` -- `queue.SimpleQueue.put` is a fast, non-blocking, GIL-protected enqueue with no callback execution; the lambda that calls `_set_queued_ids`/`self._on_change` (`_render_library`) only runs later, off this call stack entirely, on the Tk main thread via `root.after(50, self._drain_events)` (`gui.py:533`). No reentrancy is possible: the dispatched callback isn't even invoked until long after `on_queue_change` returns and the lock is released.
- **Tests using a real `SplitLibraryController`/`StemslayerApp`** (`test_history.py`'s `test_controller_propagates_queued_track_ids_in_fifo_order_as_the_running_job_drains`, `test_gui_library_rows.py`'s `QueuedLabelRenderTests`/`CloseDrainTests`) either take the same async `dispatch=self._events.put` path (real `StemslayerApp`) or the controller's *default* `dispatch=lambda callback: callback()` combined with the *default* `on_change=lambda _state: None` (no controller override in the FIFO-propagation test's constructor call). The synchronous path only ever reaches `_set_queued_ids` (a plain field replace) and a no-op `on_change`; neither touches `JobManager`.
- **`test_job_manager.py`**: `on_queue_change=queue_snapshots.append`, a bare `list.append`. Trivially reentrancy-free.
- Repo-wide search for `on_queue_change` confirms exactly these call sites (`history.py` production, and the two test files) plus the definition/tests in `job_manager.py` itself -- no other wiring exists.

No path from `on_queue_change` back into any `JobManager` method exists anywhere in the codebase today. Holding `self._lock` across the call is safe.

### 3. Why the race is genuinely closed, not just asserted

Before this commit, `submit()`'s critical section was `{lock: append + compute queued_ids}` followed by an *unlocked* `on_queue_change(queued_ids)` call. A concurrent `_pump()` on another thread (triggered by a different job finishing) could acquire the lock, pop that same just-submitted job, fire its own (correct, "promoted") report, and have that report's callback complete *before* the scheduler resumed the submitting thread to deliver its now-stale "queued" report -- because nothing serialized the two calls to `on_queue_change` relative to each other, only the state mutations were serialized.

After this commit, every call site's critical section is `{lock: mutate state + compute snapshot + call on_queue_change}`, atomically. This closes the gap for a structural reason, not merely by convention:
- `_pump()` can only promote (pop) a job that `submit()` already appended to `self._pending`. That append only becomes visible under the lock's mutual exclusion once `submit()`'s critical section has run.
- Because the report call is now *inside* the same critical section as the append, `submit()`'s report is guaranteed to have already returned (call completed) before `submit()` releases the lock -- which is a precondition for `_pump()` to even begin its own critical section (it needs the same lock to pop the entry).
- Therefore the two reports can no longer race: whichever thread's mutation happens causally later can only begin its critical section (and thus its report) after the causally-earlier thread's report has already been delivered. Delivery order now provably follows true state-change (lock acquisition) order, not scheduler luck.
- This argument holds regardless of `threading.Lock`'s lack of a fairness/FIFO guarantee for waiters, since the property being relied on is mutual exclusion plus the append-before-pop data dependency, not any particular wake order.

The same reasoning applies symmetrically to `_pump()`'s report racing a later `submit()` (a newly submitted job while another is mid-promotion) and to `shutdown()`'s report racing either: `shutdown()` sets `self._draining = True` in the same critical section as its `on_queue_change(())` call, so any `_pump()` that acquires the lock afterward observes `self._draining` and breaks before reporting again -- no stale post-shutdown report can follow the drain report.

### 4. No new contention, ordering problem, or third issue found

- **cancel() interaction**: `cancel()`'s queued-job branch already reported inside the lock before this commit (the pattern this fix generalizes); unchanged by this diff. `cancel()`'s running-job branch never calls `on_queue_change` at all (correct: cancelling a running job doesn't change `_pending`'s FIFO positions), so there is nothing for it to race against. Making the other three sites consistent with `cancel()`'s pre-existing pattern only *adds* to the total ordering guarantee across all four sites; it does not create a new ordering conflict between any pair of them.
- **Lock hold time / contention**: the call added to each critical section is `self._on_queue_change(...)`, which in production is a single non-blocking `queue.SimpleQueue.put` -- negligible added hold time, no I/O, no possibility of blocking indefinitely inside the lock. `_pump()`'s existing `_start_worker(...)` call (thread creation) was already outside the lock before this commit and remains outside it; this fix does not move any slow operation inside the critical section.
- **`_pumping` reentrancy guard**: unchanged by this diff; still prevents concurrent `_pump()` loops on multiple threads, independent of this fix.
- **shutdown() vs. concurrent submit()**: a job submitted concurrently with (or racing) `shutdown()` is a pre-existing design question (whether a submission during drain is silently appended to a queue about to be cleared) unrelated to this commit -- this diff only relocates the `on_queue_change` call one indent level inward without reordering `_draining`/`_pending.clear()` relative to each other or introducing any new interaction with `submit()`.
- No third issue found. Re-read the full `JobManager` class end to end (not just the diff hunks) looking specifically for any other place shared state is read or mutated outside a `with self._lock:` block; found none beyond what existed before this commit.

### Updated Issues Found (this pass)

CRITICAL: None (unchanged from Pass 2).

WARNING #1 from Pass 2 (**cross-thread queue-report race**): **CLOSED**. Verified structurally sound above, not merely by trusting the commit message; no deadlock risk (the only reachable callback never calls back into `JobManager`); the race's structural precondition (report delivery outside the mutual-exclusion boundary) no longer exists at any of the four call sites. Suggestion #1 from Pass 2 (add a sequence number / staleness rejection) is no longer required to close a real gap; it remains available as optional defense-in-depth but is not a residual risk today.

WARNING #2 (GUI-level `_cancel_library_track` button/confirm-dialog wiring untested), WARNING #3 (`guitar_adapter.run_specialist` D13 scenarios source-verified/generically-tested only), and WARNING #4 (tasks C.5/C.7/C.8 deferred build verification) from Pass 2 are **unchanged** and were already accepted as reasonable, non-blocking residual risk in that pass. Nothing found in this pass gives new reason to reconsider that acceptance.

### Final Verdict

**PASS.** Commit `a26d47a` closes the one WARNING this review chain had open, and closes it soundly: the fix is structurally correct (report delivery is now inside the same mutual-exclusion boundary as the state mutation it reports, for every call site), introduces no deadlock risk (the only reachable callback chain never calls back into `JobManager`), and introduces no new contention or ordering problem (the added call is a single non-blocking enqueue, and the one pre-existing lock-protected report site, `cancel()`, is now the pattern every other site follows rather than an outlier). 417/417 tests pass, re-confirmed independently in this pass with wall-clock timing consistent with the established ~30s baseline (no hang). 0 CRITICAL, 0 blocking WARNING remain. The two carried-forward PARTIAL scenarios (`guitar_adapter` D13, deliberately scoped) and the three explicitly-deferred build-verification tasks (C.5/C.7/C.8, requiring a human-run portable PyInstaller build) remain the only open items, and both were already risk-accepted in Pass 2 on their own merits, unrelated to this fix.
