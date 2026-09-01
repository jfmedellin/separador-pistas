# Tasks: Review Phase 1 — Correctness & Security Foundation

Ship order (mandated by design): **Phase A (ARC-01) → Phase B (ARC-02) → Phase C (SEC-01)**.
Each phase is one independently revertible PR. Full Review Workload Forecast at the end.

## Phase A: ARC-01 — Single-Instance Lock

- [x] A.1 Create `SeparationWorker/paths.py` with `local_data_root()` (moved from `history.py`, D4); `history.py` imports and re-exports it unchanged. Run `python -m unittest discover -s Tests\Portable -v` to confirm this pure move is behavior-preserving.
- [x] A.2 RED: `Tests/Portable/test_instance_lock.py` — a second `acquire_single_instance()` call in a spawned subprocess (`subprocess.run([sys.executable, "-c", ...])` against a temp root) returns `False` while the first process still holds the lock.
- [x] A.3 RED: same file — the lock releases after the holder process exits; a later in-process `acquire_single_instance()` against the same root succeeds.
- [x] A.4 GREEN: Create `SeparationWorker/instance_lock.py` with `acquire_single_instance(root: Path | None = None) -> bool` using `msvcrt.locking(fd, LK_NBLCK, 1)` on `local_data_root()/"instance.lock"`, `fcntl.flock` fallback (D9); hold the file handle for process lifetime. Run A.2–A.3 to green.
- [x] A.5 RED: `Tests/Portable/test_instance_lock.py` — when the lock is already held, the second-instance path never constructs `HistoryStore` and never calls `recover_unfinished()`, and no `preparing`/`processing` row is altered (inject/mock `main()`'s collaborators).
- [x] A.6 GREEN: Modify `SeparationWorker/gui.py` `main()` (~1900-1913): call `acquire_single_instance()` before `ctk.CTk()`/`HistoryStore()`; on `False`, show a throwaway `tkinter.Tk()` + `withdraw()` + `messagebox.showerror(...)` + `destroy()`, also write to `stderr`, then `return 1` — no CustomTkinter theme, no `TkinterDnD.require`, no `StemslayerApp`, no `HistoryStore`. Run A.5 to green.
- [x] A.7 Run full `python -m unittest discover -s Tests\Portable -v`; zero regressions. Ship as PR #1 (ARC-01).

## Phase B: ARC-02 — Immutable Input Copy

- [x] B.1 RED: `Tests/Portable/test_history.py` — mutating the source file after `add()` returns MUST NOT change the job's hash or separation input; fake `separate` asserts its input digest equals `tracks.source_hash`. Must fail on current `master`.
- [x] B.2 RED: same file — the immutable copy is created at `local_data_root()/inputs/{track_id}/source{ext}` before hashing; `_prepare()` no longer reads `record.source_path` after the copy step.
- [x] B.3 RED: same file — the input copy is deleted via the existing removal path (`history.py:445-461`) when the job reaches `ready`.
- [x] B.4 RED: same file — the input copy is deleted when the job reaches `failed`.
- [x] B.5 RED: same file — `retry()` re-copies from the unchanged `tracks.source_path` after a prior copy was deleted, and separation succeeds again.
- [x] B.6 RED: `Tests/Portable/test_stem_lifecycle_integration.py` — `publish_atomic` still succeeds end-to-end with the input copy in place under `local_data_root()/inputs`, never under `library_root` (D10 regression).
- [x] B.7 GREEN: Modify `SeparationWorker/history.py` `_prepare()` (559-608): at entry `rmtree` any stale `inputs/{track_id}`, `mkdir`, `copyfile(record.source_path -> inputs/{track_id}/source{ext})` (`OSError` → `DemucsSeparationError("input.copy_failed")`); read metadata and hash from the copy; call `separate(staged_input, ...)` with the copy path.
- [x] B.8 GREEN: Add `HistoryStore.discard_input_copy(track_id)` and `HistoryStore.purge_input_copies()` to `history.py`; wire `discard_input_copy` into the `ready`/`failed` terminal paths (`finally`) and into `remove()` (445-461). Run B.1–B.6 to green.
- [x] B.9 Modify `SeparationWorker/gui.py`: add a `purge_input_copies()` call next to `recover_unfinished()` (~514-515) at startup — safe now that Phase A guarantees exactly one process reaches this code.
- [x] B.10 Run full `python -m unittest discover -s Tests\Portable -v`; zero regressions, including Phase A's suite. Ship as PR #2 (ARC-02), based on PR #1.

**Apply-time deviation from design's ARC-02 sequence diagram (documented, not silent):** metadata
(`title`/`artist`/`genre`/`duration`) is read from `record.source_path` (the original path), not
from the staged copy. The formal spec (`source-identity-immutability/spec.md`) only requires the
*hash* and the *separation input* to read the immutable copy; it says nothing about metadata.
Reading metadata from the copy would rename-collide with `source{ext}`, silently dropping the
original filename as the title fallback for untagged files and breaking
`test_adds_immediately_then_publishes_durable_ready_result`'s existing `"Song"` title assertion.
Metadata is cosmetic (not part of job identity/stems), so this narrow deviation is spec-compliant.

## Phase C: SEC-01 — App-Owned Model Manager

**BLOCKING PREREQUISITE (out-of-band, not covered by any task below):** the trusted SHA-256
digests for `955717e8-8726e21a.th` and every `htdemucs_6s` bag member (`.yaml` + member `.th`)
must be supplied externally. No task here invents or substitutes placeholder hashes.
`model_manifest.py`'s `REGISTERED_MODELS` cannot hold real entries, and **PR #3 must not merge**,
until these values arrive.

- [ ] C.1 Apply-time verification: confirm the frozen worker (`runtime/worker_main.py` → `frozen_worker_path()`) forwards `--repo` verbatim to `demucs.separate`, with no argument stripping/rewriting in the frozen path.
- [ ] C.2 Apply-time verification: confirm `Tools/stemslayer_portable.spec` (PyInstaller) collects `demucs/remote/*.yaml`; add an explicit `datas`/hidden-import entry if the current build omits it — D7's bag-file copy depends on this.
- [ ] C.3 RED: `Tests/Portable/test_model_manager.py` — `ensure_model()` with a fake `registry`/`opener` fails closed with `ModelAcquisitionError("model.hash_mismatch")` on a SHA-256 mismatch; no partial repo dir survives.
- [ ] C.4 RED: same file — an unregistered model name raises `ModelAcquisitionError("model.unregistered")`.
- [ ] C.5 RED: same file — a non-`https` URL in a `ModelFile.urls` entry is rejected before any fetch attempt.
- [ ] C.6 RED: same file — a mid-download failure (truncated stream / `opener` raises) leaves no partial `models/{name}` directory.
- [ ] C.7 RED: same file — a previously verified, on-disk model is reused without calling `opener` again (assert call count == 0 on a second `ensure_model()` call).
- [ ] C.8 GREEN: Create `SeparationWorker/model_manifest.py` with frozen `ModelFile`/`ModelEntry` dataclasses and the `REGISTERED_MODELS: Mapping[str, ModelEntry]` constant — structure only; real entries wait on the blocking prerequisite above.
- [ ] C.9 GREEN: Create `SeparationWorker/model_manager.py` — `ensure_model(model_name, *, cache_root=None, registry=None, opener=None, on_progress=None) -> Path`; re-verify SHA-256 of every file on every call (D6); on miss/mismatch: `rmtree` stale dir, `mkdtemp` staging, stream `.th` files over HTTPS-only URLs with timeout + size cap, verify before placement, copy+verify bag `.yaml` from vendored `demucs/remote/` (D7), `os.replace(staging, models/{name})` (D8); define `ModelAcquisitionError(code, cause, recovery)`. Run C.3–C.7 to green.
- [ ] C.10 RED: `Tests/Portable/test_model_manager.py` (or `test_demucs_adapter.py`) — `_command()`'s argv always contains `--repo <dir>` for both the CPU call and the CUDA-fallback call (capture argv via injected `runner`).
- [ ] C.11 GREEN: Modify `SeparationWorker/demucs_adapter.py`: `_command()` (218-248) takes `repo` and emits `--repo <repo>`; `separate_audio()` calls `model_manager.ensure_model(profile.primary_model, on_progress=...)` between lines 489-492 and passes `repo` to both `_command` calls (497, 507); catch `ModelAcquisitionError`, re-raise as `DemucsSeparationError(...)` per D3. Run C.10 to green.
- [ ] C.12 Update `Tools/stemslayer_portable.spec` line 3 stale comment about Demucs' own on-use download.
- [ ] C.13 Wire the optional `on_progress` callback from `ensure_model()` through `separate_audio()` to the GUI controller hook (matching the `on_change`/`on_success` convention); exact widget binding is an apply-time detail.
- [ ] C.14 Run full `python -m unittest discover -s Tests\Portable -v`; zero regressions across all three phases. Ship as PR #3 (SEC-01), based on PR #2. Do not merge until the blocking prerequisite manifest values are supplied.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | Phase A ~150-220, Phase B ~180-260, Phase C ~420-520 (session budget is 800/PR, not the skill default 400) |
| 800-line budget risk | Low (each phase is its own PR and stays well under 800) |
| Chained PRs recommended | Yes — mandated by the design's ship order and per-defect revertibility, independent of line count |
| Suggested split | PR 1 (ARC-01) → PR 2 (ARC-02) → PR 3 (SEC-01), strictly sequential |
| Delivery strategy | ask-on-risk |
| Chain strategy | stacked-to-main — each PR merges to main in order before the next starts |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Medium (Phase C alone would flag High under the skill's generic 400-line default; under this session's explicit 800-line budget it is Low)

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | ARC-01 single-instance lock | PR 1 | `python -m unittest Tests.Portable.test_instance_lock -v` | Launch two real GUI processes against the same `local_data_root()` and confirm the second shows the error dialog and exits | Revert `gui.py` lock call + delete `instance_lock.py`; no persisted state, safe standalone revert |
| 2 | ARC-02 immutable input copy | PR 2 | `python -m unittest Tests.Portable.test_history -v` | Add a real file, mutate it mid-run, confirm stems match pre-mutation bytes | Revert `_prepare()` copy step; orphaned `inputs/` dirs are inert to an older build |
| 3 | SEC-01 model manager | PR 3 | `python -m unittest Tests.Portable.test_model_manager -v` | First-run download of `htdemucs` against a live network sandbox, confirm `--repo` invocation and hash verification | Revert `--repo` arg + delete manager module; Demucs resumes its own download path, no SQLite writes to undo |

Risk to flag explicitly: the three PRs are **not parallelizable** — PR 2 cannot land before PR 1
(the ARC-02 `purge_input_copies()` startup call is only safe once ARC-01's lock guarantees a
single process), and PR 3 is blocked on an external SHA-256 manifest input plus two apply-time
verifications (C.1, C.2). This sequential coupling, not line count, is the primary reason
`ask-on-risk` should trigger an explicit go/no-go before `sdd-apply` starts Phase C.
