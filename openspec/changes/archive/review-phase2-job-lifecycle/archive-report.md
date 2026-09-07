# Archive Report: review-phase2-job-lifecycle

## Summary

Phase 2 of `PROJECT_REVIEW_2026-08-30.md`'s recommended delivery sequence (ARC-03 + ARC-04) is complete, verified, and archived. This follows Phase 1 (`review-phase1-correctness-security` — SEC-01/ARC-01/ARC-02), already committed on the same branch but not yet archived itself (out of scope here).

## Final State

- **Branch**: `fix/review-phase1-correctness-security` (local only, not pushed).
- **Commits (5)**:
  - `cba1a78` — ARC-04: deleted `SeparationWorker/runtime/` (`supervisor.py`, `worker_main.py`, `__init__.py`) + `Tests/Portable/test_worker_runtime.py`, gated on a 3-check safety verification (repo-wide grep, import-block proof, ambiguous-call-edge resolution).
  - `c6ff9bc` — ARC-03 core: new `SeparationWorker/job_manager.py` (`JobManager`, `JobHandle`, `JobCancelled`, `run_owned`, `current_job`, Win32 job-object tree-kill via `ctypes` with silent fallback); `demucs_adapter.py`/`guitar_adapter.py` wired to `run_owned`; `SplitLibraryController` gained `cancel()`/`shutdown()`.
  - `e40ba11` — ARC-03 surface: per-row Cancel button + confirm dialog, derived "Queued" label (no schema change), `_close()` confirms once then drains via `controller.shutdown(deadline=8.0)`.
  - `1b64057` — **Bug fix found during verify, not just a test gap**: `JobManager.submit()` never reported a job's queued position until some unrelated promotion/drain moved the queue, so the *first* job queued behind a running one would show no "Queued" label at all for its entire wait, then jump straight to processing. Fixed by firing `on_queue_change` immediately on submit. Also added the 3 tests the first verify pass required (controller-level FIFO propagation, GUI-level "Queued" label render, end-to-end `cancel()` against a real killed subprocess).
  - `a26d47a` — **Second bug fix, introduced by the first fix**: the immediate report in `1b64057` fired outside the manager's lock, opening a cross-thread race where a stale "still queued" report could land after a correct "now running" report and permanently mis-display an active job as queued. Closed by moving every `on_queue_change` call site (`submit()`, `_pump()`'s promotion loop, `shutdown()`) to fire while still holding the lock that guards the state being reported, matching the pattern `cancel()` already used.
- **Tests**: 417/417 `Tests/Portable` passing (394 post-ARC-04 baseline + 20 new across ARC-03 core/surface/verify-remediation), independently re-run multiple times against `.venv-portable` by the orchestrator, not just trusted from sub-agent reports.
- **Verify verdict**: PASS, 0 CRITICAL, 0 blocking WARNING, after 3 verification passes (see `verify-report.md`, moved into this archive alongside this report).

## Accepted Residual Risk (non-blocking, explicitly documented, not silently dropped)

- `gui.py`'s cancel-button/confirm-dialog wiring is untested at the GUI-button level (controller-level `cancel()` is tested end-to-end against a real killed subprocess).
- `guitar_adapter.run_specialist`'s ownership-parity scenarios are source-verified and generically tested only (via the adapter-agnostic `run_owned`/`JobManager` tests), not with a live specialist process — `run_specialist` has zero production callers today, confirmed repo-wide.
- Tasks C.5, C.7, C.8 (apply-time verification requiring a real built portable EXE + Task Manager observation of tree-kill and the Win32 job-object fallback path) remain deferred to a human with access to a real `Tools/build_portable.ps1` build — this session has no CI/build pipeline it can invoke. Same class of caveat Phase 1's C.1/C.2 had for `--repo`.

## Capability Changes

- **Added**: `job-lifecycle` — promoted from this change's delta spec (`specs/job-lifecycle/spec.md`) into `openspec/specs/job-lifecycle/spec.md` as this repository's first main-spec capability (both `openspec/specs/` and `openspec/changes/archive/` were empty before this archive).

## Next Steps (not part of this archive)

- Decide whether to push this branch (now 8 commits total across Phase 1 + Phase 2) or open PR(s).
- Get a human to run C.7/C.8 against a real portable build before treating the change as fully field-verified.
- Phase 3 of `PROJECT_REVIEW_2026-08-30.md` (PERF-01, PERF-02, storage admission/quota) is the next recommended change and has not been started.
