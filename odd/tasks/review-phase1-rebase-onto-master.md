# Rebase the security/stability branch onto master

## Objective
Bring `fix/review-phase1-correctness-security` (Phase 1: ARC-01, ARC-02, SEC-01;
Phase 2: ARC-03, ARC-04) up to date with `origin/master` (v1.2.0) without losing
any fix and without regressing the split-library / type-scale work merged since.

## Problem / why
The branch was left local-only on 2026-09-07, 12 commits ahead and 13 behind
master. None of its fixes reached master (master commit 2f5794b is misnamed and
only adds docs). Conflicts are confined to `SeparationWorker/gui.py` and
`Tests/Portable/test_gui_library_rows.py`.

## Scope / constraints
- Rebase, resolving conflicts by keeping master's UI rewrite and re-applying the
  branch's behavior (cancel UI, queued label, bounded close-drain, instance lock,
  model manager wiring).
- Backup ref: `backup/review-phase1-pre-rebase`. Worktree:
  `separador-pistas-worktrees/review-phase1-rebase`.
- Runner: `.venv-portable` + `python -m unittest discover -s Tests/Portable`.
- TDD: off (no project config found); ordinary functional checks.
- Route: direct inline (rebase is mechanical, conflicts limited to 2 files).

## Tasks
- [x] T1 Baseline: master suite and branch suite pass before rebase (record counts).
- [x] T2 Confirm master did not already fix ARC-01/02/03/04, SEC-01 (grep evidence).
- [x] T3 `git rebase origin/master`, resolve each conflicting commit.
- [x] T4 Full suite green on the rebased branch; `compileall` clean.
- [x] T5 Manual smoke of the touched GUI surfaces (cancel button, queued label, single-instance).
- [ ] T6 Close Phase 1 SDD verify/archive; push branch and open PR.

## Evidence
- T1: master (4f16986) 397/397 OK; branch (636f49c) 417/417 OK, with
  "RuntimeError: main thread is not in main loop" ignored-exception noise.
- T2: `git grep` on origin/master finds no instance lock, model manager /
  SHA-256 manifest, JobManager, or immutable input copy; `runtime/supervisor.py`
  still present. Master commit 2f5794b only adds .gitignore + odd/tasks docs.
- T3: rebase stopped twice, both in gui.py: SEC-01 (header labels: kept
  master's `ui_font` scale, re-added the model-download status label) and
  ARC-03 surface (master moved rows to a grid: Queued label gets its own
  column 1, Cancel takes the action column 5). Folded via autosquash into
  the original ARC-03 commit. Route: direct inline.
- T4: 429/429 OK in three consecutive full runs + compileall clean.
  Root-caused an order-dependent failure in test_history
  (`test_old_content_add_during_superseded_cleanup_claims_its_own_row`):
  GUI fixtures left tk.Variable objects for a later GC cycle, which ran on a
  JobManager worker thread and deadlocked in Variable.__del__. Fixed in the
  fixtures (`gc.collect()` after destroy) -- commit 67eef30. Latent on the
  pre-rebase branch (the noise), surfaced by master's new tests.
- T5: user-run smoke on the rebased branch (2026-09-19): model download progress, Queued label, mid-run Cancel, close-drain confirmation, second instance refused. Reported OK.
- T6: branch pushed, PR #20 opened against master (2026-09-19). Phase 1 SDD verify/archive still
  open under openspec/changes/review-phase1-correctness-security.
