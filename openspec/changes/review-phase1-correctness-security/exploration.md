# Exploration — review-phase1-correctness-security

Source: `PROJECT_REVIEW_2026-08-30.md`, "Fix first" items #1-3 (SEC-01, ARC-01, ARC-02).
Scope is deliberately limited to these three findings. All other review findings
(PERF-*, QLT-*, UX-*, SEC-02/03, DAT-01, DEP-01) are out of scope and belong to
later phases of the review's recommended delivery sequence.

## Current State

### SEC-01 — Unsafe first-run model acquisition and deserialization

- This repo never calls Demucs' Python model-loading API directly. It shells out:
  `run_demucs()` (`SeparationWorker/demucs_adapter.py:112-134`) runs
  `subprocess.run([...])`, and `_command()` (`demucs_adapter.py:218-248`) builds
  `[executable, "-m", "demucs.separate", "--name", profile.primary_model,
  "--device", device, ..., "--out", str(staging), str(audio_file)]` (frozen
  builds swap `executable` for the bundled `StemslayerWorker.exe`, same argv
  shape). Two model names are used: `htdemucs` (legacy 4-stem,
  `engine/stem_profile.py:199`) and `htdemucs_6s` (metal 6-stem,
  `stem_profile.py:216,242`).
- Model loading happens *inside that subprocess*, in the vendored `demucs`
  package (`demucs/pretrained.py:get_model_from_args` -> `get_model`). Any fix
  must intercept it via subprocess argv/environment — the vulnerable call is
  third-party code we don't own and must not patch in place.
- `pretrained.get_model(name, repo=None)` (`demucs/pretrained.py:58-102`) tries
  HuggingFace first (`from .hf import get_hf_model`) before falling back to the
  legacy AWS path. `huggingface_hub` and `safetensors` are **not** in
  `Tools/requirements-portable.txt` and not installed in `.venv`/`.venv-portable`
  — so `get_hf_model` always raises `ImportError` (caught at `pretrained.py:76-82`)
  and every run falls through to `RemoteRepo`, confirming the review's evidence
  for the current dependency set.
- `RemoteRepo.get_model()` (`demucs/repo.py:56-70`) calls
  `torch.hub.load_state_dict_from_url(url, map_location='cpu', check_hash=True,
  weights_only=False)`. The URL/checksum come from `demucs/remote/files.txt`:
  for `htdemucs` the only signature is `955717e8`, file
  `hybrid_transformer/955717e8-8726e21a.th`, hosted at
  `https://dl.fbaipublicfiles.com/demucs/...`. The trailing `8726e21a` is only
  an 8-hex-char (32-bit) prefix, not an app-owned SHA-256.
- **Key finding**: Demucs already ships a first-class offline path that needs no
  upstream patch: `LocalRepo` (`demucs/repo.py:76-111`) scans a directory for
  `{sig}.th` / `{sig}-{checksum}.th` files (and `.yaml` bag files for
  `BagOnlyRepo`), and `separate.py --repo <dir>` (`add_model_flags`,
  `separate.py:31-37`, threaded to `get_model(name, repo=args.repo)`) makes
  Demucs load **only** from that local directory — `RemoteRepo`/`torch.hub` is
  never reached. `LocalRepo` does its own `check_checksum()` (`repo.py:29-40`)
  against the filename-embedded prefix (same weak strength as today), but we
  control exactly what bytes are ever placed at that path.
- Nothing in this repo currently passes `--repo`; every run today is fully
  exposed to the network/pickle path.
- Zero test coverage: `Tests/Portable/test_demucs_adapter.py` has no
  model/download/hash/`--repo` tests.

### ARC-01 — Multi-instance recovery corrupts live job state

- `SeparationWorker/gui.py:main()` (lines 1900-1913) is the sole entry point:
  `root = ctk.CTk()` -> `TkinterDnD.require(root)` -> `StemslayerApp(root)` ->
  `root.mainloop()`. No instance check exists anywhere before or inside this.
- `StemslayerApp.__init__` (`gui.py:463-531`) unconditionally does
  `self.history_store = HistoryStore()` then
  `self.history_store.recover_unfinished()` (lines 514-515) — every launch, no
  guard.
- `HistoryStore.recover_unfinished()` (`history.py:381-387`) is a single
  unconditional `UPDATE tracks SET status='interrupted' ... WHERE status IN
  ('preparing','processing')` — no notion of whose job it is.
- Schema confirmed via `_initialize()` (`history.py:138-182`):
  `SCHEMA_VERSION = 2` (`history.py:121`), migrated in two `PRAGMA
  user_version` steps — v1 creates `tracks` (no owner/lease/heartbeat columns,
  `status` CHECK-constrained to 6 known values), v2 adds `identity_claims`
  (content-identity dedup only, not ownership). This is a clean, established
  migration pattern (`if version < N: executescript(...); PRAGMA user_version =
  N`) that a v3 lease migration should follow exactly, if that path is chosen.
- No `pywin32`, `portalocker`, or any locking library is in
  `Tools/requirements-portable.txt` — the project is stdlib-only for
  Windows-specific tricks elsewhere (e.g. `subprocess.CREATE_NO_WINDOW` via
  `getattr(subprocess, ...)` in `demucs_adapter.py:128`).
- Storage root is `local_data_root()` (`history.py:35-39`) ->
  `%LOCALAPPDATA%/Stemslayer`, created in `HistoryStore.__init__` — a natural,
  already-writable location for a lock file.
- Existing coverage: `Tests/Portable/test_history.py:test_recovers_unfinished_and_preserves_missing_results`
  only asserts single-process recovery semantics. No test simulates a second
  concurrent instance/process.

### ARC-02 — Mutable-input identity race can associate the wrong stems

- `SplitLibraryController.add()` (`history.py:535-541`) creates the catalog row
  synchronously then starts a daemon thread (`_start_thread`, `history.py:474-475`)
  running `_prepare()`.
- `_prepare()` (`history.py:559-608`): line 566 `digest =
  self._hash_source(requested_source)` hashes `record.source_path` (the live
  user file path, not a copy); line 567 `owner =
  self.store.claim_identity(track_id, digest, profile.pipeline_fingerprint)`
  claims that hash inside a `BEGIN IMMEDIATE` transaction (`claim_identity`,
  `history.py:255-341` — cross-thread/cross-process serialization point, no
  file-copy involved). Then at line 594,
  `self._separate(record.source_path, record.result_directory, profile=profile)`
  is called — **the same `record.source_path`**, read again later.
- `separate_audio()` (`demucs_adapter.py:433-...`) does `audio_file =
  Path(audio_file).resolve()` (line 474) and only checks `audio_file.is_file()`
  — it re-reads whatever bytes are at that path at inference time, which can
  differ from what `source_sha256()` (`history.py:42-47`, streamed SHA-256
  reader) hashed moments earlier if the user edits/replaces the file in
  between.
- No copy step exists anywhere between hash and inference today —
  `separate_audio` stages Demucs' own *output* under
  `tempfile.TemporaryDirectory(prefix="stemslayer-demucs-")` (line 492), but
  never stages the *input*.
- Natural placement for an immutable input copy: `HistoryStore.library_root` is
  `local_data_root() / "library"` (`history.py:126`), and each track's durable
  output already lives at `record.result_directory` under that root. Cleanest
  placement: a job-owned staging path alongside/under that same root — e.g.
  `library_root / track_id / "input<ext>"` — copied once at the *start* of
  `_prepare` (before `_hash_source`), then both `_hash_source` and `_separate`
  operate on that copy, never on `requested_source`/`record.source_path` again.
- Existing coverage: no test in `test_history.py` or
  `test_stem_lifecycle_integration.py` mutates the source file between hash and
  separation. Zero regression coverage today.

## Affected Areas

- `SeparationWorker/demucs_adapter.py` — `_command()` (218-248) needs a
  `--repo <verified-dir>` argument once acquisition is app-owned; `run_demucs()`
  is the natural place to reason about network denial (in practice, `--repo`
  structurally prevents `RemoteRepo` from ever running, which is the real gate).
- `SeparationWorker/gui.py` — `main()` (1900-1913) needs single-instance/lease
  acquisition before `StemslayerApp(root)`; `StemslayerApp.__init__` (514-515)
  is where `recover_unfinished()` is called unconditionally today.
- `SeparationWorker/history.py` — `HistoryStore._initialize()` (138-182) for a
  `SCHEMA_VERSION = 3` migration if lease-based recovery is ever chosen;
  `recover_unfinished()` (381-387); `_prepare()` (559-608) for the immutable
  copy step.
- A new model-manager module (does not exist yet) — owns full-SHA-256/manifest
  verification, placement into a `LocalRepo`-shaped directory, and is the only
  thing that ever performs the network fetch.
- `Tools/requirements-portable.txt` / `Tools/stemslayer_portable.spec` —
  reference points only (spec comment at line 3: "No model weights are
  collected; Demucs downloads htdemucs on use" — needs updating once
  acquisition changes).
- `Tests/Portable/test_history.py`, `test_demucs_adapter.py`,
  `test_stem_lifecycle_integration.py` — zero coverage for these three defects
  today; new tests belong here.

## Approaches Considered

### SEC-01

1. **Dedicated model manager + Demucs `--repo` local-directory redirection**
   (recommended). Download each model ourselves, verify a full app-owned
   SHA-256 manifest, atomically place `.th`/`.yaml` files into a
   `LocalRepo`-shaped directory, always invoke `demucs.separate --repo <dir>`.
   No new runtime dependency; bypasses `RemoteRepo`/`torch.hub`'s
   pickle-capable, weakly-hashed path entirely; reuses Demucs' own trusted
   local loader. Trade-off: still calls a pickle-based `torch.load` internally
   on the `.th` file — full SHA-256 verification prevents *tampering* but
   doesn't eliminate the pickle format itself. Effort: Medium.
2. Migrate to HuggingFace/safetensors (`get_hf_model`). Not pickle-based, but
   adds two new runtime dependencies, no app-owned hash contract, still a live
   network call unless pre-populated, larger diff. Effort: Medium-High.
3. Post-hoc verification only (fallback, not recommended) — detects but
   doesn't prevent the deserialization exposure, since `torch.hub.load_state_dict_from_url(...,
   weights_only=False)` already executes before our check would run. Effort: Low.

### ARC-01

1. **Hard single-instance enforcement via a stdlib exclusive lock**
   (recommended) — e.g. `msvcrt.locking()` on a file under
   `local_data_root()`, or a Win32 named mutex via
   `ctypes.windll.kernel32.CreateMutexW`, acquired at the top of `main()`
   before `HistoryStore()`/`recover_unfinished()` run; on failure, show a
   message and exit without touching history. Matches the review's stated
   preference; OS releases the lock automatically on process death; no schema
   change, no new dependency. Effort: Low.
2. Lease/heartbeat ownership in SQLite (`SCHEMA_VERSION = 3`) — fallback only,
   for if concurrent instances become a real product requirement. Real added
   complexity (heartbeat thread, clock-skew, crash-without-release handling).
   Effort: Medium-High.

### ARC-02

1. **Immutable job-owned input copy before hash+inference** (recommended, and
   the review's stated preferred design) — copy `record.source_path` into a
   job-owned staging file in `_prepare()` before hashing, then hash+separate
   that copy. Closes the race entirely; the copy can be cleaned up alongside
   the existing removal path (`history.py:445-461`). Effort: Medium.
2. Re-hash after inference, discard/retry on mismatch — explicitly named by
   the review as a fallback, not the preferred design; wastes inference work
   on mismatch. Effort: Low.

## Recommendation

- **SEC-01**: Approach 1 (dedicated model manager targeting Demucs'
  `LocalRepo`/`--repo`). No new dependency, reuses a path Demucs already ships
  and tests, satisfies the review's ask directly.
- **ARC-01**: Approach 1 (hard single-instance lock at `main()`), per the
  review's own stated preference and this app's current single-window desktop
  scope.
- **ARC-02**: Approach 1 (immutable job-owned input copy), exactly as the
  review's preferred design, placed under the existing `library_root`/
  `result_directory` layout.

## Risks / Open Questions

- SEC-01's "app-owned full SHA-256" needs an actual trusted source of truth for
  the digest of `955717e8-8726e21a.th` (and the `htdemucs_6s` bag's members) —
  that manifest must be produced/verified out-of-band before implementation.
  No network fetch was attempted during this read-only exploration.
- The three defects share one hot file (`SeparationWorker/history.py` —
  `_prepare`/`claim_identity`/`recover_unfinished`/`_initialize` all in one
  class). ARC-01's schema-v3 path (if ever chosen) and ARC-02's copy-step both
  touch `_prepare()`; sequencing/PR-splitting needs care to avoid one large
  diff (this concentration is QLT-05 in the source review — out of scope to
  fix here, but relevant to task sequencing).
- Zero existing regression tests cover any of the three defects today —
  sdd-tasks should budget for new tests, not just behavior changes.

## Ready for Proposal

Yes. All three defects, their exact code locations, and a recommended approach
per defect are established with concrete evidence.
