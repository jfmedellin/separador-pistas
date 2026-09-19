# Tasks: Review Phase 2 — Job Lifecycle (ARC-03 + ARC-04)

Ship order (mandated by design): **Phase A (ARC-04, delete `runtime/`) → Phase B (ARC-03 core) →
Phase C (ARC-03 surface)**. Each phase is one independently revertible PR. Full Review Workload
Forecast at the end.

## Phase A: ARC-04 — Delete `SeparationWorker/runtime/`

Apply-time verification (design's C.1) is a **safety gate that MUST run and pass BEFORE any
deletion task**, not a formality performed afterward.

- [x] A.1 Apply-time verification C.1, check 1 (repo-wide grep): run
      `rg -n "SeparationWorker\.runtime|from \.runtime|import runtime" --glob '!Tests/**' --glob '!openspec/**' .`
      and confirm zero hits outside the files being deleted.
- [x] A.2 Apply-time verification C.1, check 2 (import-block proof): run
      `python -c "import sys; sys.modules['SeparationWorker.runtime']=None; import SeparationWorker.gui, SeparationWorker.cli, SeparationWorker.history, SeparationWorker.demucs_adapter, SeparationWorker.guitar_adapter, SeparationWorker.demucs_worker; print('ok')"`
      and confirm it prints `ok` (proves no production module imports `runtime` even dynamically).
- [x] A.3 Apply-time verification C.1, check 3 (resolve the ambiguous CodeGraph `run` edge): run
      `rg -n "Supervisor|supervisor" SeparationWorker/demucs_adapter.py SeparationWorker/guitar_adapter.py`
      and confirm zero hits — falsifies "real caller" and confirms the edge is `subprocess.run`.
- [x] A.4 Only after A.1–A.3 all pass: delete `SeparationWorker/runtime/__init__.py`,
      `SeparationWorker/runtime/supervisor.py`, `SeparationWorker/runtime/worker_main.py`.
- [x] A.5 Delete `Tests/Portable/test_worker_runtime.py` (exercises only removed code; spec
      scenario "Removed test leaves no remaining reference").
- [x] A.6 Run full `python -m unittest discover -s Tests\Portable -v`; confirm zero regressions
      against the 411-test baseline (spec scenario "Static scan finds zero production importers").
      Ship as PR #1 (ARC-04).

## Phase B: ARC-03 core — `JobManager`, adapter wiring, controller wiring

RED tests for `job_manager.py` (Testing Strategy table, unit rows 1–4):

- [x] B.1 RED: `Tests/Portable/test_job_manager.py` — a fifth `submit()` at default concurrency 1
      queues instead of being rejected; jobs start in FIFO order; `on_queue_change` reports the
      exact order. Must fail on current `master` (module does not exist yet).
- [x] B.2 RED: same file — `terminate()` then process exit inside the grace period → `kill()`
      never called; no exit within grace → `kill()` called exactly once. Use the `ScriptedProcess`
      fake shape (the same one the deleted `test_worker_runtime.py` used) injected into the
      escalation helper.
- [x] B.3 RED: same file — `shutdown(deadline)` returns `False` within the deadline (never blocks
      past deadline + slack) when a job body waits on a never-set `Event`.
- [x] B.4 RED: same file — `run_owned()` with no current job behaves exactly like `subprocess.run`
      against a real short-lived `sys.executable -c` process (CLI/`Tools/` path unaffected).
- [x] B.5 GREEN: Create `SeparationWorker/job_manager.py` with `JobCancelled`, `JobHandle`
      (`register`/`unregister`), `current_job()` bound via `threading.local` (D5), `JobManager`
      (`collections.deque` + `threading.Lock` + non-reentrant `_pump()`, D2; `start_worker=`
      injectable, D3), `run_owned()` (token check at entry and exit, D8; terminate→5s grace→kill
      against a Win32 job object via `ctypes`, silent fallback to plain `Popen` on any Win32
      failure, D9), `shutdown(deadline)` (one shared monotonic budget, default 8.0s, never raises,
      D10). Run B.1–B.4 to green.
- [x] B.6 GREEN: Modify `SeparationWorker/demucs_adapter.py` `run_demucs` (113-135):
      `subprocess.run(list(command), **options)` becomes `run_owned(list(command), **options)`;
      options dict, `_cpu_process_environment`, `CREATE_NO_WINDOW` unchanged.
- [x] B.7 GREEN: Modify `SeparationWorker/guitar_adapter.py` `run_specialist` (162-177): identical
      one-line `run_owned` swap (D13 — zero production callers today, verified repo-wide; wired
      anyway so resolved decision #3 holds the moment a specialist profile is bound).
- [x] B.8 D7 regression test (Testing Strategy, integration row 5): `Tests/Portable/test_demucs_adapter.py`
      — an injected `runner` that sets the token and raises `JobCancelled` during the CUDA attempt
      propagates `JobCancelled` (not `CalledProcessError`) and the CPU fallback runner is never
      invoked (asserts call count == 1 for the CUDA runner, 0 for the CPU one). Proves cancel does
      NOT trigger `separate_audio:512-521`'s CUDA-failure-triggers-CPU-fallback path.
- [x] B.9 Regression test (Testing Strategy, integration row 6): same file — a child that writes to
      stdout then exits non-zero still reaches `_diagnostic_tail` with the same output after the
      `Popen` swap (`communicate()`'s `stdout=PIPE, stderr=STDOUT` capture preserved).
- [x] B.10 RED: `Tests/Portable/test_history.py` (Testing Strategy, integration row 7) — cancel
      mid-run lands on `status="interrupted"` with the `job.cancelled` detail, leaves no
      `inputs/{track_id}` and no `library_root/{track_id}`, and `retry()` on that row still
      succeeds. Must fail on current `master`.
- [x] B.11 RED: same file (Testing Strategy, integration row 8) — `commit_if_active` blocks the
      commit when the token is set just before publication, via a `validate=` hook passed into
      `publish_atomic`.
- [x] B.12 RED: same file (Testing Strategy, integration row 9) — `SplitLibraryController.shutdown(deadline)`
      against a fake `separate` cancels one running job and drains three queued jobs; the queued
      jobs never start a subprocess; call returns within the bounded deadline.
- [x] B.13 GREEN: Modify `SeparationWorker/history.py`: `SplitLibraryController.__init__` builds a
      `JobManager(start_worker=..., on_queue_change=...)` (D3, D4); `add()`/`retry()` (581-603)
      call `submit()` instead of `_start_worker`; `_prepare()` (605-675) takes `handle`, passes
      `cancellation=handle.token` to `separate`, gains an `except JobCancelled` /
      cancelled-`PublicationError` branch → `status="interrupted"`,
      `error_detail="job.cancelled ..."` (D12), inserted before the generic `except Exception` →
      `failed`; add `cancel(track_id)`, `shutdown(deadline)`, `_queue_changed`; `LibraryState`
      gains `queued_track_ids: tuple[str, ...] = ()`. Run B.10–B.12 to green.
- [x] B.14 Run full `python -m unittest discover -s Tests\Portable -v`; zero regressions,
      including Phase A's post-deletion baseline. Ship as PR #2 (ARC-03 core), based on PR #1.

## Phase C: ARC-03 surface — cancel UI, Queued label, bounded close-drain

- [x] C.1 RED: `Tests/Portable/test_gui_library_rows.py` (or a new sibling test module following
      its `StemslayerApp` + real `Tk` root fixture pattern) — a job body that blocks past the
      grace/kill window; calling `_close()` with the confirmation auto-accepted proves no
      child thread/process survives past the bounded deadline, and `root.destroy()` fires only
      after the drain completes, never before. Must fail on current `master` (no drain exists yet).
      **Documented gap:** the design's File Changes table does not enumerate a new GUI-level test
      file for this scenario — Testing Strategy row 9 only names the `SplitLibraryController`-level
      test (covered by B.12). This task adds the missing GUI-level regression the design's own
      spec scenario "No child process survives close" requires.
      Implemented as two tests in `Tests/Portable/test_gui_library_rows.py`:
      `CloseDrainTests` (real `subprocess.Popen` registered on the job handle; confirms killed
      after `_close()`, and `root.destroy` is only called after `shutdown()` returns) and
      `CloseConfirmationDeclinedTests` (D11: a declined confirm leaves `_closed` `False` and the
      window/job alive). Confirmed both fail on current `master` (`askyesno` never called, 0
      times) before C.2's implementation.
- [x] C.2 GREEN: Modify `SeparationWorker/gui.py`: add a per-row Cancel action wired to
      `SplitLibraryController.cancel(track_id)`, gated by a confirmation dialog stating the work
      is lost and not resumable (D11); modify the library render path to show a derived "Queued"
      label over rows whose `track_id` is in `state.queued_track_ids` while status stays
      `preparing` (no new `tracks.status` value); modify `_close()` (1901-1914) to confirm once
      when work is in flight (D11) before setting `self._closed = True`, then call
      `controller.shutdown(deadline=8.0)` (D10) before `root.destroy()`; a declined confirmation
      returns with `_closed` still `False`. Run C.1 to green.
      Implemented: new `stop` icon glyph + per-row Cancel button (`_cancel_library_track`,
      `messagebox.askyesno` confirm) for `preparing`/`processing` rows; a "Queued" label rendered
      when `record.track_id in state.queued_track_ids`; new `_work_in_flight()` helper
      (`preparing`/`processing` in `state.tracks`) gating `_close()`'s confirm; `_close()` now
      calls `self.library_controller.shutdown(deadline=8.0)` before `root.destroy()`. C.1 is green.
- [x] C.3 Run full `python -m unittest discover -s Tests\Portable -v`; zero regressions.
      414/414 passed (412-test baseline + the 2 new C.1 tests), 28.3s.

Apply-time verification (design's C.2) — **requires a real portable build to execute, same
caveat Phase 1's C.1/C.2 had.** Distinct task set, not folded into C.2's RED/GREEN pair:

- [x] C.4 Apply-time verification: `StemslayerApp --self-test` (`gui.py:1929-1932`) still resolves
      the worker and exits 0. **Verified in dev venv** (not source-inference only): ran
      `main(['--self-test'])` directly against `.venv-portable` — returned exit code `0`. Note:
      this exercises only the `sys.argv == ["--self-test"]` short-circuit; the `frozen_worker_path()`
      call inside `if getattr(sys, "frozen", False):` is unreachable without an actual PyInstaller
      build, so the worker-executable resolution itself is unverified here. Code at this path is
      byte-identical to Phase B's baseline (Phase C touched only `gui.py`'s library render/close
      methods, not `main()`).
- [ ] C.5 Apply-time verification: no console window flashes during a split — confirms
      `CREATE_NO_WINDOW` survived the `Popen` swap.
      **Source-level confirmation only** (per apply instructions, this is Phase B's concern and
      already unchanged): `grep -n "CREATE_NO_WINDOW"` in `demucs_adapter.py:130` and
      `guitar_adapter.py:175` — both present, unmodified by this Phase C diff (Phase C did not
      touch either adapter file). The actual "no visible flash" observation still needs a real
      portable build + a live split to confirm visually — **left unchecked; needs a human on a
      built portable EXE**.
- [x] C.6 Apply-time verification: force a Demucs failure — the error detail still shows a
      diagnostic tail, confirming `communicate()` preserved the `stdout=PIPE, stderr=STDOUT`
      capture `subprocess.run` gave.
      **Already covered by Phase B's B.9 regression test** —
      `Tests/Portable/test_demucs_adapter.py::test_cuda_and_cpu_failure_reports_bounded_diagnostic_tails`,
      re-confirmed passing in this run's full-suite pass (C.3). Not re-tested, cited per instructions.
- [ ] C.7 Apply-time verification: cancel a CPU run started with `--jobs > 0` — Task Manager
      shows `StemslayerWorker.exe` **and every child it spawned** gone within the grace period
      (D9's actual tree-kill claim; threat-matrix row "Process integration / child-tree
      containment").
      **CANNOT be done from a dev shell.** Requires a real portable (PyInstaller) build run with
      `--jobs > 0`, a live cancel, and Task Manager observation of the process tree. Left
      unchecked — needs a human to run against a built portable EXE.
- [ ] C.8 Apply-time verification: fault-inject the Win32 job-object helper to force the fallback
      path — cancel still terminates the direct child, and close still completes within the
      deadline.
      **CANNOT be done from a dev shell.** Requires a real portable build with the `ctypes`
      job-object helper fault-injected (e.g. forcing `_create_and_assign_job_object` to return
      `None`) and an observed live cancel/close against the built EXE. Left unchecked — needs a
      human to run against a built portable EXE.
- [ ] C.9 Ship as PR #3 (ARC-03 surface), based on PR #2. Not shipped by this run — left for the
      orchestrator to commit; C.7/C.8 should be scheduled before this PR is considered mergeable,
      per the Review Workload Forecast below.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | Phase A ~560-600 (near-pure deletion: 226+51+3 source lines + 279 test lines), Phase B ~550-750 (new `job_manager.py` + `test_job_manager.py` ~150-250 each, `history.py`/`test_history.py` wiring ~150-250, small adapter swaps ~10-20, D7/D6-row regression tests ~40-70), Phase C ~250-400 (session budget is 800/PR, not the skill default 400) |
| 800-line budget risk | Phase A Low (pure deletion, mechanically reviewable), Phase B Medium-High (new module + two test files + controller rewiring is the densest slice), Phase C Low-Medium |
| Chained PRs recommended | Yes — mandated by the design's own three-slice ship order and independent revertibility, independent of line count |
| Suggested split | PR 1 (ARC-04 delete `runtime/`) → PR 2 (ARC-03 core: `JobManager` + adapters + controller) → PR 3 (ARC-03 surface: cancel UI + close-drain), strictly sequential |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main — each PR merges to main in order before the next starts |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Medium (Phase B alone would flag High under the skill's generic 400-line
default; under this session's explicit 800-line budget it is Medium, driven by one new module,
two new/modified test files, and controller rewiring landing in a single PR)

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | ARC-04 delete dead `runtime/` scaffolding | PR 1 | `python -m unittest discover -s Tests\Portable -v` | N/A — pure deletion, no runtime behavior to exercise beyond the C.1 import-block proof (A.2) | Restore the three deleted `runtime/*.py` files and `test_worker_runtime.py`; no other file touched, trivially revertible |
| 2 | ARC-03 core: `JobManager`, `run_owned`, adapter + controller wiring | PR 2 | `python -m unittest Tests.Portable.test_job_manager Tests.Portable.test_history Tests.Portable.test_demucs_adapter -v` | Real multi-track `add()` sequence against a live `SplitLibraryController` (concurrency 1, four sources) confirming FIFO start order and a real cancel that reaches a live subprocess | Revert the `run_owned` swap in both adapters back to `subprocess.run`; delete `job_manager.py`; revert `history.py`'s `JobManager` build, `submit`/`cancel`/`shutdown`; queue/cancel state is memory-only, no DB migration to undo |
| 3 | ARC-03 surface: cancel UI, Queued label, bounded close-drain | PR 3 | `python -m unittest Tests.Portable.test_gui_library_rows -v` | Real GUI session: queue four real splits, cancel one from the row action, close the window mid-run, confirm no `StemslayerWorker.exe` or specialist process survives (C.4-C.8 on a real portable build) | Remove the per-row Cancel action, the Queued label render, and `_close()`'s drain call; `_close()` reverts to its prior immediate-destroy behavior; PR 2's `JobManager` remains usable headless |

Risk to flag explicitly: the three PRs are **not parallelizable** — PR 2 cannot land before
PR 1 (deleting `runtime/` first removes the only code path C.1's ambiguous CodeGraph `run` edge
could plausibly resolve to, so the safety gate must clear before any new lifecycle code lands),
and PR 3's cancel UI has nothing to wire against until PR 2's `JobManager`/`cancel()`/`shutdown()`
exist. C.2's apply-time verification (C.4-C.8) additionally requires a **real portable build**,
not just the `.venv`/`.venv-portable` unit-test environment — the same caveat Phase 1's C.1/C.2
apply-time verifications carried, and it should be scheduled before PR 3 is considered mergeable,
not treated as optional follow-up. Under `auto-chain`, the orchestrator proceeds directly with PR 1
using the stacked-to-main chain strategy; no user go/no-go gate blocks the start of implementation.
