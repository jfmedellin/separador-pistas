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

**BLOCKING PREREQUISITE — RESOLVED 2026-09-03.** Both `.th` weights were downloaded directly
from Demucs' own trusted source (`https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/...`,
resolved via the vendored `demucs/remote/files.txt`) and hashed with `sha256sum`. Each bag
(`htdemucs.yaml` → `models: ['955717e8']`, `htdemucs_6s.yaml` → `models: ['5c90dfd2']`) has exactly
one `.th` member, so no multi-file bag logic is needed. The `.yaml` hashes are of the vendored
files already in `.venv` / `.venv-portable` (D7: copied, not downloaded) and match byte-for-byte
across both venvs, confirming the frozen build ships the same bytes.

| file_name | sha256 |
|---|---|
| `955717e8-8726e21a.th` | `8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4` |
| `htdemucs.yaml` | `239c445d0b14454d541ad8bd9bb271c9e536d267e8a4625208744cbb2e7bb66c` |
| `5c90dfd2-34c22ccb.th` | `34c22ccb381c6f9fdbf324f04e1e2fe21aaaf293f5ded163a162697ff9a02ddd` |
| `htdemucs_6s.yaml` | `207405151270af8fd81c2373c25d27950916682ac91dca7884a11ce13dad6f58` |

The `8726e21a` / `34c22ccb` filename suffixes are Demucs' own convention (first 8 hex chars of the
true sha256) and match these computed digests exactly — independent confirmation the downloaded
bytes are genuine and unmodified. `model_manifest.py`'s `REGISTERED_MODELS` can now hold real
entries; C.3–C.14 are unblocked.

- [x] C.1 Apply-time verification: confirm the frozen worker (`runtime/worker_main.py` → `frozen_worker_path()`) forwards `--repo` verbatim to `demucs.separate`, with no argument stripping/rewriting in the frozen path.
- [x] C.2 Apply-time verification: confirm `Tools/stemslayer_portable.spec` (PyInstaller) collects `demucs/remote/*.yaml`; add an explicit `datas`/hidden-import entry if the current build omits it — D7's bag-file copy depends on this.
- [x] C.3 RED: `Tests/Portable/test_model_manager.py` — `ensure_model()` with a fake `registry`/`opener` fails closed with `ModelAcquisitionError("model.hash_mismatch")` on a SHA-256 mismatch; no partial repo dir survives.
- [x] C.4 RED: same file — an unregistered model name raises `ModelAcquisitionError("model.unregistered")`.
- [x] C.5 RED: same file — a non-`https` URL in a `ModelFile.urls` entry is rejected before any fetch attempt.
- [x] C.6 RED: same file — a mid-download failure (truncated stream / `opener` raises) leaves no partial `models/{name}` directory.
- [x] C.7 RED: same file — a previously verified, on-disk model is reused without calling `opener` again (assert call count == 0 on a second `ensure_model()` call).
- [x] C.8 GREEN: Create `SeparationWorker/model_manifest.py` with frozen `ModelFile`/`ModelEntry` dataclasses and the `REGISTERED_MODELS: Mapping[str, ModelEntry]` constant, populated with the real, independently-verified hashes from the blocking prerequisite above.
- [x] C.9 GREEN: Create `SeparationWorker/model_manager.py` — `ensure_model(model_name, *, cache_root=None, registry=None, opener=None, on_progress=None, bundled_root=None) -> Path`; re-verify SHA-256 of every file on every call (D6); on miss/mismatch: `rmtree` stale dir, `mkdtemp` staging (created inside `cache_root`, matching `publish_atomic`'s own same-volume convention for atomic `os.replace`), stream `.th` files over HTTPS-only URLs with a timeout and size cap, verify before placement, copy+verify bag `.yaml` from a vendored `demucs/remote/`-shaped directory (D7), `os.replace(staging, models/{name})` (D8); define `ModelAcquisitionError(code, cause, recovery)`. Ran C.3–C.7 to green.
- [x] C.10 RED: `Tests/Portable/test_model_manager.py` — `_command()`'s argv always contains `--repo <dir>` for both the CPU call and the CUDA-fallback call (capture argv via injected `runner`).
- [x] C.11 GREEN: Modified `SeparationWorker/demucs_adapter.py`: `_command()` takes a required keyword-only `repo` and emits `--repo <repo>` right after `--name`; `separate_audio()` calls `model_manager.ensure_model(profile.primary_model, on_progress=on_progress)` right after the reusable-result short-circuit and passes `repo` to both `_command` calls (CPU and CUDA-fallback); catches `ModelAcquisitionError`, re-raises as `DemucsSeparationError(code, cause, recovery)` per D3. Ran C.10 to green; updated `Tests/Portable/test_demucs_adapter.py`'s existing `_command()`/`separate_audio()` tests accordingly (see deviations below) — zero regressions.
- [x] C.12 Updated `Tools/stemslayer_portable.spec` line 3 stale comment about Demucs' own on-use download; also documented, next to the `collect_all("demucs")` loop, that it already sweeps `demucs/remote/*.yaml` with no `excludes` (verified at apply time — both `htdemucs.yaml` and `htdemucs_6s.yaml` are present in the collected datas).
- [x] C.13 Wired the optional `on_progress` callback from `ensure_model()` through `separate_audio()` to `SplitLibraryController`'s new `on_model_progress` constructor hook (matching the `on_change`/`on_success` convention; relayed through `self._dispatch` since it fires on the background worker thread) and from there to a new minimal `StemslayerApp._model_download_progress` GUI binding (a small status label under the library header).
- [x] C.14 Ran full `python -m unittest discover -s Tests\Portable -v` (both `.venv` and `.venv-portable`); 411/411 passing (398 Phase A/B baseline + 13 new Phase C tests), zero regressions across all three phases. Ready to ship as PR #3 (SEC-01), based on PR #2.

**Apply-time findings and deviations from design (documented, not silent):**

1. **C.1 — design's file reference was inexact.** The design says the frozen worker is
   `runtime/worker_main.py` → `frozen_worker_path()`. In fact `frozen_worker_path()`
   resolves to `StemslayerWorker.exe`, built by `Tools/stemslayer_portable.spec` from
   `SeparationWorker/demucs_worker.py` (`runtime/worker_main.py` is unrelated scaffolding
   used only by `test_worker_runtime.py`). `demucs_worker.py:main(argv)` calls
   `demucs.separate.main(argv)` with the argv list untouched — no stripping or rewriting —
   which satisfies C.1's requirement regardless of the file-name discrepancy.
2. **C.2 — no code change was needed.** `collect_all("demucs")` already collects every
   non-`.py` data file under the installed `demucs` package with no `excludes`, which was
   confirmed at apply time to include all 11 `demucs/remote/*.yaml` files (including
   `htdemucs.yaml` and `htdemucs_6s.yaml`) plus `files.txt`. Only a clarifying comment was
   added; D7's bag-file copy depends on this and it already holds.
3. **`model_manager.ensure_model()` gained one parameter beyond the design's four:
   `bundled_root: Path | None = None`.** It defaults to the real vendored
   `demucs/remote/` directory (`Path(demucs.__file__).parent / "remote"`), so every
   production call site is unaffected. It exists purely so `Tests/Portable/test_model_manager.py`
   can point D7's bundled-file copy step at a throwaway directory instead of the real
   vendored path — the same testability rationale the design already gives for
   `cache_root`/`registry`/`opener`.
4. **`separate_audio()` gained one new keyword-only parameter: `on_progress`,** forwarded
   verbatim to `model_manager.ensure_model()`. This was necessary for C.13's wiring and is
   additive (default `None`), so no existing caller breaks.
5. **`Tests/Portable/test_demucs_adapter.py` needed updating, not just `demucs_adapter.py`.**
   Every existing test that calls `_command()` directly now passes `repo=`, and the whole
   module patches `model_manager.ensure_model` (via `setUpModule`/`tearDownModule`) to a
   fixed fake path, because `separate_audio()` now calls real model acquisition on every
   run and none of that module's ~40 pre-existing tests exercise acquisition itself (that
   coverage lives in `test_model_manager.py`). Without this, every pre-existing test in
   that file would have attempted a real network download on first run.
6. **`SplitLibraryController.__init__` gained one new keyword-only parameter:
   `on_model_progress`,** defaulting to a no-op, matching the `on_change`/`on_success`
   convention exactly. `history.py`'s own tests (`test_history.py`) were unaffected because
   every fake `separate` callable there already accepts `**_options`.
7. **Do not merge until the blocking prerequisite manifest values are supplied** — this
   condition is now satisfied; the real SHA-256 values are in `model_manifest.py`.

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
