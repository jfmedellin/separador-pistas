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
- [ ] T1 Baseline: master suite and branch suite pass before rebase (record counts).
- [ ] T2 Confirm master did not already fix ARC-01/02/03/04, SEC-01 (grep evidence).
- [ ] T3 `git rebase origin/master`, resolve each conflicting commit.
- [ ] T4 Full suite green on the rebased branch; `compileall` clean.
- [ ] T5 Manual smoke of the touched GUI surfaces (cancel button, queued label, single-instance).
- [ ] T6 Close Phase 1 SDD verify/archive; push branch and open PR.

## Evidence
(filled per task)
