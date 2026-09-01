# Proposal: Review Phase 1 — Correctness & Security Foundation

## Intent

Phase 1 of `PROJECT_REVIEW_2026-08-30.md` "Fix first" (SEC-01, ARC-01, ARC-02). All three silently produce unsafe or wrong results, and none has any regression test today.

- **SEC-01**: no run passes `--repo`, so weights load via `torch.hub.load_state_dict_from_url(..., weights_only=False)` (`demucs/repo.py:56-70`), verified only by an 8-hex filename prefix. A compromised mirror executes arbitrary code on first run.
- **ARC-01**: every launch runs `recover_unfinished()` (`gui.py:514-515` → `history.py:381-387`), flipping all `preparing`/`processing` rows to `interrupted` with no owner. A second instance corrupts the first's live jobs; the user sees healthy splits reported as failed.
- **ARC-02**: `_prepare()` hashes the live user file (`history.py:566`) then separates that same mutable path (`history.py:594`). Editing the file in between binds new-content stems to an old identity claim, poisoning dedup permanently.

## Scope

### In Scope
- App-owned model manager: fetch, verify against a full SHA-256 manifest, atomically place `.th`/`.yaml` into a `LocalRepo`-shaped dir; `_command()` always passes `--repo <dir>`.
- Single-instance stdlib exclusive lock in `gui.py:main()` acquired **before** `HistoryStore()`/`recover_unfinished()`; second instance exits with a message, touching no history.
- Immutable job-owned input copy at the top of `_prepare()`; hash and separate only the copy; cleanup via the existing removal path (`history.py:445-461`).
- Regression tests for all three in `Tests/Portable/`.

### Out of Scope
- All other review findings (PERF-*, QLT-*, UX-*, SEC-02/03, DAT-01, DEP-01) — later phases.
- Multi-instance concurrency as a feature (lease/heartbeat schema v3).
- HuggingFace/safetensors migration; removing torch's pickle format.
- Editing the vendored `demucs` package.

## Capabilities

### New Capabilities
- `model-integrity`: verified, app-owned model acquisition and offline local-repo loading.
- `instance-exclusivity`: exactly one running instance owns history recovery.
- `source-identity-immutability`: stems bind to the exact bytes that were hashed.

### Modified Capabilities
- None (`openspec/specs/` is empty).

## Approach

Exploration Approach 1 for each; none adds a runtime dependency.

1. **SEC-01** — `--repo <verified-dir>` structurally prevents `RemoteRepo`/`torch.hub` from running, reusing a loader Demucs already ships. Full SHA-256 replaces the 32-bit prefix as trust anchor.
2. **ARC-01** — an OS-level lock releases automatically on process death; no schema change.
3. **ARC-02** — copying before hashing makes hash and inference read identical, app-owned bytes.

## Constraints

- No new runtime dependency (`Tools/requirements-portable.txt` stays stdlib-only for OS tricks).
- No edits to the vendored `demucs` package.
- Any schema change MUST follow the existing `SCHEMA_VERSION`/`PRAGMA user_version` pattern (`history.py:121,138-182`). None is expected.
- Argv-list subprocess calls, no `shell=True`.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| new model-manager module | New | Manifest verification + local-repo placement; sole network fetch |
| `SeparationWorker/demucs_adapter.py` | Modified | `_command()` (218-248) adds `--repo` |
| `SeparationWorker/gui.py` | Modified | `main()` (1900-1913) acquires lock before app init |
| `SeparationWorker/history.py` | Modified | `_prepare()` (559-608) copies input before hashing |
| `Tools/stemslayer_portable.spec` | Modified | Stale comment (line 3) about on-use download |
| `Tests/Portable/` | New | Regression tests for all three defects |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| No trusted source for the app-owned SHA-256 manifest yet | High | Produce/verify digests out-of-band before apply; block SEC-01 until pinned |
| Input copy doubles disk use per job | Med | Delete copy on completion via existing removal path |
| Three fixes concentrate in `history.py` (QLT-05) | Med | Split into one PR per defect; ARC-02 owns `_prepare()` |
| Lock file leaks after hard kill | Low | Use OS-released locks, never a presence-only sentinel file |
| First run now blocks on our fetch, not Demucs' | Med | Explicit progress/failure UX; fail closed, never fall back to `RemoteRepo` |

## Rollback Plan

Portable EXE shipped via release tags — rollback means republishing a prior tag, so each fix must be independently revertible.

- **SEC-01**: revert the `--repo` argument and the manager module; Demucs resumes its own download path. Verified weights already on disk are inert. No data written to SQLite, so no migration to undo.
- **ARC-01**: revert the lock in `main()`; behavior returns to today's unguarded recovery. No persisted state; stale lock files are harmless and ignored by an older build.
- **ARC-02**: revert the copy step; `_prepare()` resumes hashing `record.source_path`. Copies already written under `library_root` are orphaned files an older build ignores — document a manual cleanup note rather than a destructive migration.
- Cross-cutting: ship as three separate PRs/commits so any single defect fix can be reverted without dropping the other two.

## Resolved Product Decisions

The following open questions were raised during proposal review and resolved by the user before spec/design:

1. **Model verification failure** → fail closed always. No user override. A hash mismatch or fetch failure blocks separation entirely; no fallback to `RemoteRepo`.
2. **Model acquisition** → download on first use (per profile), not pre-bundled. Same UX shape as today, but with app-owned verification and a progress indicator instead of Demucs' own silent download.
3. **Second instance behavior** → show a message and exit. No focus/raise-existing-window IPC in this phase.
4. **Immutable input copy lifecycle** → delete on job completion via the existing removal path (`history.py:445-461`). Do not retain it for later re-splits — avoids adding unbounded per-job storage, consistent with the review's DAT-01 concern (out of scope to fix here, but not worth reintroducing).

## Dependencies

- Trusted SHA-256 digests for `955717e8-8726e21a.th` and the `htdemucs_6s` bag members (out-of-band prerequisite for SEC-01).

## Success Criteria

- [ ] No code path can reach `RemoteRepo`/`torch.hub.load_state_dict_from_url`; weights load only from the verified local repo.
- [ ] A hash mismatch fails the run closed, with no fallback download.
- [ ] A second instance launch cannot alter any `preparing`/`processing` row.
- [ ] Mutating the source file after `add()` cannot bind mismatched stems to the original digest.
- [ ] New `Tests/Portable/` tests fail against current `master` and pass after the change.
- [ ] `python -m unittest discover -s Tests\Portable -v` passes.
