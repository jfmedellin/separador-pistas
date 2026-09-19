# Stemslayer Project Review — 2026-08-30

## Executive summary

Stemslayer has unusually strong low-level invariants for a young desktop application: atomic stem publication, parameterized SQL, argv-only subprocess execution, bounded protocol frames, explicit profile contracts, and a broad portable test suite. The largest risks are now **above** those primitives: job lifecycle, production wiring, release provenance, accessibility, and duplicated UI/application paths.

This review found **9 high-priority**, **13 medium-priority**, and **4 low-priority** improvement areas. No application source or configuration was changed. This report is the only added file.

### Fix first

1. Make the downloaded model an application-owned, fully verified artifact before loading it.
2. Introduce a single-instance policy or job leases, plus a bounded/cancellable separation queue.
3. Remove the mutable-input hash/inference race by separating an immutable job copy.
4. Add PR/push CI; the current tests run only after a release tag is pushed.
5. Replace mouse-only custom controls and restore visible error/destructive-action feedback.
6. Stream publication and validation instead of holding complete stems in memory.

## Scope and confidence

| Item | Reviewed target |
| --- | --- |
| Production snapshot | `origin/master` at `21dc301`, tagged `v1.0.0` |
| Local checkout | `master` at `fdba7da`, 9 commits behind the production ref |
| Review mode | Read-only source/configuration inspection; no checkout, fetch, install, or runtime mutation |
| Structural analysis | CodeGraph first, then targeted `git show`, `git grep`, AST hotspot analysis, and workflow/dependency inspection |
| External validation | Context7 plus current official PyTorch, Python/pip, GitHub, Microsoft, W3C, CustomTkinter, PyInstaller, and PyPI documentation |
| Test execution | 358 portable tests passed on the unchanged local checkout in 21.323s |
| Production test caveat | `origin/master` contains 379 portable test methods, but that snapshot was not checked out or executed to preserve the read-only constraint |

Severity means expected impact, not implementation effort:

- **High:** can produce wrong results, loss of control, arbitrary code execution after supply-chain compromise, release-quality failure, or exclusion from core workflows.
- **Medium:** meaningful reliability, maintainability, performance, security-hardening, or usability gap.
- **Low:** limited-scope improvement or documented operational/privacy debt.

## Findings ledger

| ID | Severity | Area | Finding |
| --- | --- | --- | --- |
| SEC-01 | High | Security | First-run model loading uses executable deserialization without an application-owned full digest |
| ARC-01 | High | Correctness | A second app instance marks active work from the first instance as interrupted |
| ARC-02 | High | Correctness | Hashing and inference read the same mutable source at different times |
| ARC-03 | High | Reliability | Separation jobs are unbounded, non-cancellable, and not shut down cleanly |
| PERF-01 | High | Performance | Publication materializes complete stems multiple times in memory |
| QLT-01 | High | Reliability | One failing UI callback can permanently stop the event pump |
| QLT-02 | High | Delivery | CI runs only on release tags, not on pull requests or pushes |
| UX-01 | High | Accessibility | Core custom controls are mouse-only and lack accessible names/focus states |
| UX-02 | High | UX | Add-song errors are written into a permanently hidden banner |
| SEC-02 | Medium | Supply chain | Build inputs and GitHub Actions references are versioned but not immutable |
| SEC-03 | Medium | Distribution | Portable executables are unsigned and releases have no provenance attestation |
| ARC-04 | Medium | Architecture | Tested worker supervision and network isolation are not used by production |
| PERF-02 | Medium | Performance | Startup deeply rereads every ready stem library entry |
| QLT-03 | Medium | Configuration | Development setup omits `mutagen` and silently degrades metadata |
| QLT-04 | Medium | Architecture | Hidden legacy separation UI and tests coexist with the durable library path |
| QLT-05 | Medium | Maintainability | GUI, history, and separation hotspots have excessive responsibilities/complexity |
| QLT-06 | Medium | Observability | Broad exception handling has no production logging path |
| UX-03 | Medium | Safety | Remove performs immediate irreversible recursive deletion without confirmation/undo |
| UX-04 | Medium | Responsive UI | Fixed geometry clips at common small displays/scaling levels |
| UX-05 | Medium | Accessibility | Status is primarily color-coded and muted text misses normal-text contrast |
| UX-06 | Medium | UX | Export reports unconditional success and the overlay lacks dialog focus behavior |
| DAT-01 | Medium | Storage/privacy | Durable stems have no quota/free-space admission and metadata retention is undisclosed |
| DEP-01 | Low | Dependencies | `soundfile` is one minor release behind; upgrade should be evaluated, not blindly applied |
| UX-07 | Low | UX | Filter state is icon-only, sticky, and poorly discoverable |
| UX-08 | Low | IA | Navigation mixes destinations with the contextual Export action |
| QLT-07 | Low | Tooling | No repository-wide lint/type/coverage configuration or dependency lock exists |

## High-priority findings

### SEC-01 — Unsafe first-run model acquisition and deserialization

**Evidence**

- Model weights are intentionally omitted from the portable bundle and downloaded on first use: `Tools/stemslayer_portable.spec:1-3`, `README.md:30`.
- The live command selects a remote Demucs model by name: `SeparationWorker/demucs_adapter.py:218-248`.
- The bundled Demucs 4.1.0 loader calls `torch.hub.load_state_dict_from_url(..., check_hash=True, weights_only=False)` in `demucs/repo.py:63-70`.
- Bundled Torch extracts the digest prefix from the filename and passes it to download verification: `torch/hub.py:78-79, 890-898`. The model filenames provide only an 8-hex-character prefix.

**Impact**

If the model publishing/delivery chain is compromised, the application loads a pickle-capable checkpoint as the current Windows user. A 32-bit digest prefix is not an application-owned full integrity contract. PyTorch explicitly treats untrusted models as untrusted code.

**Recommendation**

Acquire each model through a dedicated model manager, validate an application-owned full SHA-256 or signed manifest, and only then expose it to inference. Prefer a non-executable weight format or a checkpoint compatible with current safe-loading guidance. Make normal separation network-denied after acquisition.

Reference: [PyTorch security policy](https://github.com/pytorch/pytorch/security).

### ARC-01 — Multi-instance recovery corrupts live job state

**Evidence**

- Every app startup unconditionally calls `recover_unfinished()`: `SeparationWorker/gui.py:514-515`.
- Recovery updates every `preparing` or `processing` row to `interrupted`: `SeparationWorker/history.py:381-387`.
- The schema stores no owner process, lease, or heartbeat: `SeparationWorker/history.py:146-180`.

**Impact**

Opening a second window changes the first window's active job to `interrupted`; retry/remove controls can then act on work that is still running.

**Recommendation**

For the current desktop scope, enforce a single application instance. If concurrent instances are a product requirement, persist `owner_id` plus an expiring lease/heartbeat and recover only expired ownership.

### ARC-02 — Mutable-input identity race can associate the wrong stems

**Evidence**

- `_prepare()` hashes `record.source_path` and claims the content identity: `SeparationWorker/history.py:559-567`.
- The same path is read later by separation: `SeparationWorker/history.py:581-595`.

**Impact**

Replacing or editing the audio between those reads can bind stems generated from new bytes to the old hash. A later import of the old content may incorrectly reuse those stems.

**Recommendation**

Copy the source into an immutable job-owned input first, then hash and separate that copy. A cheaper fallback is to hash again after inference and discard/retry on mismatch, but it wastes expensive work and still complicates ownership.

### ARC-03 — No bounded, cancellable job lifecycle

**Evidence**

- Every Add starts a new daemon thread immediately: `SeparationWorker/history.py:474-475, 535-542`.
- Demucs runs with blocking `subprocess.run` and no timeout/owned process handle: `SeparationWorker/demucs_adapter.py:112-134`.
- Cancellation reaches atomic publication, not inference: `SeparationWorker/demucs_adapter.py:433-443, 555-560`.
- App close shuts down mixer/cache only: `SeparationWorker/gui.py:1847-1860`.

**Impact**

Rapid imports can start multiple CPU/GPU-heavy jobs, exhaust memory, and continue as unmanaged child processes when the GUI exits. Users cannot cancel a mistaken or hung separation.

**Recommendation**

Create one `JobManager` with a bounded queue, concurrency 1 by default, explicit job state, cancellation, and `Popen` ownership with terminate/kill escalation. Increase CPU concurrency only from measured resource admission, never per click.

### PERF-01 — Whole-file memory amplification

**Evidence**

- `_assemble_lanes()` reads every output WAV into `bytes`: `SeparationWorker/demucs_adapter.py:385-398`.
- Publication writes those bytes, then validates files with additional full reads: `SeparationWorker/engine/publication.py:160-205`.
- Input admission checks existence, not size/duration/channel/rate: `SeparationWorker/demucs_adapter.py:474-481`.

**Impact**

Long/high-rate audio can hold PyTorch/NumPy data plus multiple complete stem copies at once, leading to paging or process termination.

**Recommendation**

Publish from staged paths, stream copy/hash/compare in bounded chunks, and retain atomic directory rename. Add duration/size/channel/rate admission and a disk/memory estimate before inference.

### QLT-01 — UI event pump can die permanently

**Evidence**

`_drain_events()` executes queued callbacks inside a block that catches only `queue.Empty`; scheduling the next drain occurs after the loop: `SeparationWorker/gui.py:1862-1870`.

**Impact**

Any exception in a render/controller callback escapes before `root.after(...)`. All subsequent UI state updates stop, leaving the application apparently frozen or stale.

**Recommendation**

Isolate each callback exception, report/log it, and reschedule the pump in `finally`. Add a regression test with one failing callback followed by a valid callback.

### QLT-02 — No pre-merge CI gate

**Evidence**

- The only workflow triggers on `v*.*.*` tags: `.github/workflows/windows-portable-release.yml:3-6`.
- It runs only `Tests/Portable`: `.github/workflows/windows-portable-release.yml:40, 60`.
- No PR/push workflow, lint, type check, coverage gate, or Admission-suite step exists.

**Impact**

Broken code can merge and only be discovered after a release tag starts a large build. The tag then exists even when build/tests fail.

**Recommendation**

Add a lightweight PR/push workflow for Portable + Admission tests, compile/import checks, Ruff, incremental typing, and a pragmatic coverage ratchet. Keep CPU/CUDA packaging on tags.

### UX-01 — Core custom controls are inaccessible

**Evidence**

- `_RoundButton` is a frame/canvas with only `<Button-1>` activation; it has no focus enrollment, Enter/Space binding, accessible name, or visible focus: `SeparationWorker/gui.py:272-346`.
- It powers transport, filter, Open/Retry/Remove, and other core actions: `SeparationWorker/gui.py:784-786, 880-900, 1072-1154`.
- Add song is a group of clickable layout widgets rather than a button: `SeparationWorker/gui.py:746-759`.
- Timeline seeking is pointer-only: `SeparationWorker/gui.py:1048-1054, 1162-1167, 1261-1266`.

**Impact**

Keyboard-only users cannot complete primary workflows, and assistive technology receives little semantic information.

**Recommendation**

Use real focusable controls where possible. Otherwise create one accessible custom-control abstraction with name/role/state, Tab order, Enter/Space activation, high-contrast focus ring, and tooltips. Add keyboard transport shortcuts.

References: [Microsoft Windows accessibility guidance](https://learn.microsoft.com/en-us/windows/apps/develop/accessibility), [W3C keyboard accessibility](https://www.w3.org/WAI/fundamentals/accessibility-principles/).

### UX-02 — Add-song errors are invisible

**Evidence**

- `status_banner` and its legacy controls are permanently hidden: `SeparationWorker/gui.py:723-733`.
- `_confirm_library_add()` catches errors and writes only to the hidden status variables: `SeparationWorker/gui.py:1388-1396`.

**Impact**

Database, profile, path, and file-selection failures can leave users with no visible explanation or recovery action.

**Recommendation**

Render cause and recovery in the profile dialog or a visible library-level error banner. Restore focus to the triggering control and keep terminology consistent through success/failure states.

## Medium-priority findings

### SEC-02 — Mutable release inputs

- Actions use mutable major tags (`actions/checkout@v7`, `actions/setup-python@v7`): `.github/workflows/windows-portable-release.yml:18-23`.
- The job upgrades pip and resolves dependencies online: `.github/workflows/windows-portable-release.yml:35-40, 55-60`.
- Direct versions are pinned, but transitive dependencies and hashes are not locked: `Tools/requirements-portable.txt:4-11`.
- `contents: write` applies to the whole build/release job: `.github/workflows/windows-portable-release.yml:8-14`.

Pin actions to full commit SHAs, generate a fully resolved hash-locked wheel set, use `pip --require-hashes --only-binary :all:` where compatible, and split read-only build from minimal-permission publishing. GitHub states full SHAs are the only immutable Action reference; pip's secure-install guidance requires hashes for the full dependency graph.

References: [GitHub secure use](https://docs.github.com/en/actions/reference/security/secure-use), [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/).

### SEC-03 — No executable signing or build provenance

The build creates ZIP checksums but performs no SignTool/Artifact Signing step and emits no GitHub artifact attestation or SBOM: `Tools/build_portable.ps1:111-149`, `.github/workflows/windows-portable-release.yml:66-79`.

Checksums detect accidental corruption but do not independently authenticate an artifact when the same release channel publishes both. Sign the executables consistently and attest the ZIPs. Microsoft notes unsigned downloads can trigger SmartScreen/Smart App Control friction; GitHub attestations bind artifact provenance to repository/workflow/commit.

References: [Microsoft SmartScreen reputation](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation), [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations).

### ARC-04 — Production bypasses its tested supervisor

`runtime/supervisor.py` implements identity, timeout, cancellation, termination, and environment cleanup, but has no production caller. `runtime/worker_main.py:39-59` is also unreferenced. The frozen worker instead directly invokes Demucs: `SeparationWorker/demucs_worker.py:8-13`; the GUI path uses blocking `subprocess.run`.

Wire one supervised worker path into production, or remove the dormant protocol/supervisor so tests and architecture claims describe what actually ships. Do not maintain a security boundary that exists only in tests.

### PERF-02 — Deep library validation on every startup

Startup launches `_validate_library`: `SeparationWorker/gui.py:526-531, 946-948`. `HistoryStore.validate_ready()` loads every ready session: `SeparationWorker/history.py:389-407`; session loading calculates waveform peaks by reading every frame: `SeparationWorker/engine/stem_session.py:42-70, 143-187`.

Use cheap manifest/stat validation at startup. Perform deep validation and waveform calculation lazily, cache peaks by file fingerprint, and invalidate only changed entries.

### QLT-03 — Development setup silently omits metadata support

`Tools/requirements-portable.txt:9` includes `mutagen==1.48.1`, but `Tools/setup_windows.ps1:42-47` does not install it. `read_metadata()` catches every exception and silently falls back to filename/Unknown artist: `SeparationWorker/history.py:64-89`. There is no focused metadata test.

Use one runtime dependency source for setup and packaging. Catch missing dependency separately from malformed user metadata, log/report the former, and test valid/malformed/missing-reader behavior.

### QLT-04 — Hidden legacy application path and stale tests

The app constructs `SeparationController` and the legacy profile/action/status widgets, then hides the widgets with `pack_forget()`: `SeparationWorker/gui.py:509-513, 710-733`. The visible flow uses `_choose_profile()` and `SplitLibraryController.add()`: `SeparationWorker/gui.py:1334-1396`. Tests still heavily exercise the hidden selector/action instead of the real Add song journey.

Retire the legacy path and replace those tests with Add song → profile dialog → processing → ready/error journeys. If the legacy flow remains a supported mode, expose and name it explicitly.

### QLT-05 — Responsibility and complexity hotspots

- `StemslayerApp` spans `SeparationWorker/gui.py:463-1870` and owns layout, navigation, dialogs, history, export, mixer, threading, and cleanup.
- `history.py` combines SQLite repository (`:118-461`) and job orchestration (`:478-660`).
- AST hotspots include `demucs_adapter.separate_audio` (128 lines), `runtime.supervisor.run` (95 lines/27 branch nodes), and `gui._draw_icon` (102 lines/23 branch nodes).

Extract `LibraryView`, `SplitView`, `MixerView`, and dialog/presenter components; keep the app as composition root. Split repository from job coordinator. Add characterization tests before extraction—do not introduce a new framework merely to reorganize code.

### QLT-06 — Weak observability

Production contains many broad `except Exception` boundaries and no `logging` usage. Representative silent/degraded paths include metadata (`history.py:71-83`), mixer callbacks (`mixer_controller.py:157-163`), and command dispatch (`mixer_controller.py:291-365`).

Add privacy-aware structured local logging with stable error codes and operation context. Narrow catches where possible; boundaries may catch broadly, but they must preserve diagnostics and surface actionable state.

### UX-03 — Destructive remove has no guardrail

The trash icon directly calls remove: `SeparationWorker/gui.py:880-886, 1406-1418`. The store recursively deletes the managed result directory before deleting the row: `SeparationWorker/history.py:445-461`.

Require confirmation naming the song and consequences, or preferably implement soft-delete with time-bounded Undo. Give the icon an accessible label/tooltip.

### UX-04 — Fixed geometry clips on common displays/scaling

Library/separation content uses fixed 600/820px placed panels: `SeparationWorker/gui.py:599-605, 736-739`. The app forces `960x825` and minimum height 825 while acknowledging non-scrollable clipping: `SeparationWorker/gui.py:1592-1605`.

Make separation/library scrollable, compute the work area, reflow at breakpoints, and test 800×600 and 1366×768 at 100/125/150/200% scaling. CustomTkinter documents using grid weights plus `sticky="nsew"` for filling scrollable layouts.

Reference: [CustomTkinter scrollable-frame documentation](https://github.com/tomschimansky/customtkinter/wiki/CTkScrollableFrame).

### UX-05 — Status and contrast accessibility gaps

- Ready/failed use green/red; all other states share one accent dot, and the detail omits status: `SeparationWorker/gui.py:901-926`.
- `muted2=#7A756F` (`gui.py:38`) is used for 9–10px text. Measured contrast is approximately 4.28:1 on the window and 4.00:1 on the surface, below the 4.5:1 normal-text target.

Add visible text badges for every state, progress/cancel where meaningful, and redundant non-color encoding. Raise the muted token luminance and add automated token contrast checks.

Reference: [Microsoft accessible text requirements](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessible-text-requirements).

### UX-06 — Export feedback and overlay semantics

`_poll_export_dialog()` always says “Export complete,” even when every row failed, and exposes internal error codes without recovery: `SeparationWorker/gui.py:1563-1585`. The overlay is a placed frame with no initial focus, Escape close, focus restoration, or modal protection: `SeparationWorker/gui.py:1439-1469`.

Report aggregate outcomes (`Exported 3 of 4`, `Export failed`), preserve actionable error recovery, and implement real dialog focus entry/restore, Escape, and underlying-control disablement.

### DAT-01 — Unbounded storage and undisclosed retained metadata

The library stores absolute source paths, SHA-256, tags, timestamps, and durable stems under `%LOCALAPPDATA%/Stemslayer`: `SeparationWorker/history.py:35-39, 93-108, 123-130, 190-205`. There is no quota, free-space preflight, usage view, or retention disclosure.

Add storage estimation/admission, user-visible usage, cleanup controls, and documentation of location/content/retention. Consider whether the absolute source path is still needed after successful publication.

## Low-priority improvements

### DEP-01 — Evaluate SoundFile 0.14

The project pins `soundfile==0.13.1`; PyPI lists 0.14.0 as current as of this review. Other direct pins checked—Demucs 4.1.0, CustomTkinter 6.0.0, tkinterdnd2 0.6.2, sounddevice 0.5.6, and PyInstaller 6.22.2—were current. Upgrade SoundFile only after playback/session/packaging compatibility tests; currency alone is not a reason to change a working audio stack.

Reference: [SoundFile releases on PyPI](https://pypi.org/project/soundfile/).

### UX-07 — Filter discoverability

The icon-only filter popover has no tooltip, applied-count badge, Escape/outside dismissal, or clear-all affordance: `SeparationWorker/gui.py:779-831`. Add those behaviors and restore focus to the trigger on close.

### UX-08 — Mixed navigation semantics

Top navigation combines `SPLIT`/`MIXER` destinations with `EXPORT`, which opens an overlay and may be disabled; Mixer also has a second Export action: `SeparationWorker/gui.py:550-588, 1030-1035, 1658-1661`. Keep navigation for destinations and make Export one contextual Mixer action with visible prerequisites.

### QLT-07 — Missing quality-tool baseline

No `pyproject.toml`, Ruff/mypy/pyright configuration, coverage ratchet, or full transitive dependency lock exists. Adopt these incrementally at boundaries and changed files; avoid a disruptive big-bang typing rewrite.

## Dead code and duplication

### Confirmed

1. `SeparationWorker/runtime/worker_main.py:39-59` (`run_worker`) has no caller in `origin/master`.
2. The visible production UI cannot reach the legacy start button/profile/status path because those controls are immediately hidden; the path remains constructible and directly exercised by tests.

### Suspected—validate external consumers before deletion

1. Much of `gui_controller.py` belongs to the previous transient-cache architecture. The app still instantiates `SeparationController` and reads profile information, so the entire module is not confirmed dead.
2. `protocol/selftest.py` and `engine/fixture_harness.py` are **not** dead; test documentation and test utilities reference them.

## Strengths to preserve

- Subprocess commands use argv lists and no `shell=True` was found.
- SQL values are parameterized; dynamic history sort columns are whitelisted.
- Publication validates relative paths and uses staged atomic directory rename.
- Protocol framing caps messages at 1 MiB and validates canonical structure.
- Profile/manifest/cache identities are explicit and extensively tested.
- Real-widget tests cover lane reachability and library row actions.
- Current visual language is domain-specific and coherent; the problem is interaction/accessibility, not lack of personality.
- Direct dependency pins are mostly current; the larger gap is transitive immutability/provenance, not indiscriminate package age.

## Recommended delivery sequence

1. **Correctness/security foundation:** SEC-01, ARC-01, ARC-02.
2. **One real job pipeline:** ARC-03 + ARC-04 + removal of legacy/dead runtime paths.
3. **Resource safety:** PERF-01, PERF-02, storage admission/quota.
4. **Delivery gate:** QLT-01 regression test, PR CI, hash-locked dependencies, immutable Actions, signing/attestation.
5. **Accessible interaction pass:** UX-01, UX-02, UX-03, UX-04, UX-05, UX-06.
6. **Maintainability extraction:** split GUI/history responsibilities only after the behavior is pinned by journey tests.

This ordering addresses wrong-result and trust-boundary risks before cosmetic restructuring. The alternative—refactoring the 1,700-line GUI first—would create a large review surface while the job and release contracts remain unsafe.

## Verification notes

Command executed against the unchanged local checkout:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v
```

Result: `Ran 358 tests in 21.323s — OK`.

The working tree remained clean before this report was added. Static tools such as Ruff/Vulture were not installed, so dead-code/complexity conclusions were derived from CodeGraph, repository-wide Git search, and Python AST inspection rather than installing new tooling.

## External references

- [PyTorch security policy](https://github.com/pytorch/pytorch/security)
- [GitHub Actions secure use](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
- [GitHub dependency graph and lock-file guidance](https://docs.github.com/en/code-security/concepts/supply-chain-security/dependency-graph-data)
- [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/)
- [Microsoft SmartScreen reputation](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
- [Microsoft Windows app accessibility](https://learn.microsoft.com/en-us/windows/apps/develop/accessibility)
- [Microsoft accessible text requirements](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessible-text-requirements)
- [W3C accessibility principles](https://www.w3.org/WAI/fundamentals/accessibility-principles/)
- [W3C focus order guidance](https://www.w3.org/WAI/WCAG22/Understanding/focus-order.html)
- [CustomTkinter scrollable-frame documentation](https://github.com/tomschimansky/customtkinter/wiki/CTkScrollableFrame)
- [PyInstaller stable documentation](https://pyinstaller.org/en/stable/)
- [SoundFile releases on PyPI](https://pypi.org/project/soundfile/)
