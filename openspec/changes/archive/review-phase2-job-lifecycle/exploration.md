# Exploration: ARC-03 (bounded/cancellable job lifecycle) + ARC-04 (dormant supervisor)

SDD exploration for change `review-phase2-job-lifecycle` (project separador-pistas, artifact_store hybrid).
Source: `PROJECT_REVIEW_2026-08-30.md`, delivery-sequence Phase 2 (ARC-03 + ARC-04 + confirmed-dead-code removal).
Precedent: `openspec/changes/review-phase1-correctness-security/` (SEC-01/ARC-01/ARC-02, committed on
`fix/review-phase1-correctness-security`, 411/411 Tests/Portable passing).

## Current State

**Job spawning (`SeparationWorker/history.py`)**
- `SplitLibraryController.add()` (history.py:581-588) and `retry()` (history.py:590-603) both call
  `self._start_worker(lambda: self._prepare(...))` unconditionally. Default `_start_worker` is
  `_start_thread` (history.py:507-508): `threading.Thread(daemon=True).start()` — a brand-new thread
  per call, no queue, no semaphore, no pool. Confirmed by repo-wide grep: zero occurrences of
  `Semaphore`/`Queue`/`concurrency`/`ThreadPool` anywhere under `SeparationWorker/`. Nothing bounds
  concurrent jobs.
- `_prepare()` (history.py:605-675) is the actual job body: stage input copy → hash → claim identity →
  `self._separate(...)` (blocking) → update status. `finally` always discards the staged input copy.
  No cancellation token is ever constructed or referenced anywhere in `history.py` — zero grep matches
  for `cancel`/`cancellation`/`CancellationToken` in that file.

**Subprocess execution (`SeparationWorker/demucs_adapter.py`)**
- `separate_audio()` (438-575) calls `runner(_command(...))` where the default `runner=run_demucs`.
  `run_demucs()` (113-135) is a blocking `subprocess.run(list(command), check=True, stdout=PIPE,
  stderr=STDOUT, ...)` — no timeout, no `Popen` handle retained by the caller, stdout+stderr combined
  into one text buffer. On CUDA failure it re-runs the same blocking call for CPU fallback.
- `_command()` (219-253) is frozen-build-aware: when `sys.frozen`, argv[0] becomes
  `frozen_worker_path()` → `StemslayerWorker.exe` (built from `SeparationWorker/demucs_worker.py`),
  otherwise `sys.executable -m demucs.separate`.
- `demucs_worker.py` (the frozen worker's actual entrypoint) is 13 lines: it just calls
  `demucs.separate.main(argv)` and returns. It speaks no protocol at all — no hello frame, no progress
  frame, no cancel-message handling, nothing JSON-framed.
- `separate_audio()` accepts a `cancellation=None` kwarg forwarded only to `publish_atomic(...)`
  (`engine/publication.py`), which checks `cancellation.cancelled` at 3 points around staging/commit
  (the "reaches atomic publication" the review cites). But no production caller ever constructs a
  `CancellationToken` and passes it in — `history.py._prepare()` calls
  `self._separate(staged_input, record.result_directory, profile=profile, on_progress=...)` with no
  `cancellation` argument at all. Cancellation doesn't reach publication in practice today; it's
  plumbed-but-unused dead capability, one level worse than the review's evidence implies.

**App close (`SeparationWorker/gui.py`)**
- `StemslayerApp._close()` (1901-1914): closes `mixer_controller`, discards cache directories via
  `stem_cache.discard(...)`, destroys the root window. No reference to `SplitLibraryController`, no
  join on any worker thread, no tracking of any `Popen` handle (there are none to track). Zero grep
  matches for `cancel`/`Cancel` in `gui.py` — there is no cancel button/action anywhere in the UI today.

**The dormant supervisor (`SeparationWorker/runtime/`)**
- `runtime/` has exactly 3 files: `__init__.py` (exports `Supervisor`, `WorkerFailure`, `WorkerResult`),
  `supervisor.py`, `worker_main.py`. No production `Process` implementation exists.
- `Supervisor.run(process, job, *, cancelled, publish)` (supervisor.py:164-258) is a real, well-tested
  state machine: validates a canonical protocol-v1 frame sequence (exactly one `hello` first, exactly
  one terminal frame), enforces `hello_timeout=30s`, `silence_timeout=30s` (no heartbeat/progress for
  30s → `worker.hung`, `process.terminate()`), and on cancellation escalates `cancel` message → wait
  `cancellation_grace=5s` → `terminate()` → wait `kill_grace=2s` → `kill()`.
- `process` is a duck-typed protocol requiring `.poll_frame()`, `.send(message)`, `.is_alive()`,
  `.terminate()`, `.kill()`, `.exit_code`. The only implementation in the entire repo is
  `ScriptedProcess` inside `Tests/Portable/test_worker_runtime.py` (a test fake). No `Popen`-backed
  production `Process` class exists anywhere.
- `runtime/worker_main.py`'s `run_worker()` (39-59, the code the parent review names as confirmed dead)
  calls `install_network_denial()`, which monkey-patches `socket.socket`/`socket.create_connection`
  inside the current process — only sensible for an in-process worker model. This is structurally
  incompatible with the actual shipped architecture, where the worker is a separate frozen executable
  (`StemslayerWorker.exe`) launched as a subprocess. `runtime/worker_main.py` appears designed for a
  different worker topology than what the codebase actually ships, not just "not yet wired."
- `runtime_messages()` (worker_main.py:14-36) documents the expected wire shape (`hello` →
  `progress`/`heartbeat`* → `completed`/`failed`), also with no production caller.

## Affected Areas

- `SeparationWorker/history.py` — `SplitLibraryController.add/retry/_start_thread` (job spawning, no bound/queue)
- `SeparationWorker/demucs_adapter.py` — `run_demucs`, `_command`, `separate_audio` (blocking subprocess, unused `cancellation` param, frozen-worker argv selection)
- `SeparationWorker/demucs_worker.py` — frozen worker entrypoint; speaks no protocol, cannot be cancelled mid-run except by killing the process
- `SeparationWorker/gui.py` — `StemslayerApp._close`, `main()` (no job/thread/process shutdown on close, no cancel UI)
- `SeparationWorker/runtime/supervisor.py`, `worker_main.py`, `__init__.py` — tested but production-uncalled; `worker_main.py`'s in-process network-denial model conflicts with the actual out-of-process worker
- `SeparationWorker/engine/publication.py` — `CancellationToken`/`publish_atomic` cancellation hooks (reachable mechanism, currently never invoked from the real job path)
- `Tests/Portable/test_worker_runtime.py` — only place a `Process` protocol implementation exists (`ScriptedProcess`, a fake)
- `SeparationWorker/guitar_adapter.py` — a second, structurally identical blocking-subprocess adapter (`run_specialist`, `_cancelled`) that would need the same treatment for consistency if a `JobManager`/wired supervisor is introduced

## Coupling between ARC-03 and ARC-04

The parent review bundles them for a real reason, but the coupling is looser than "fixing one requires
the other":

- ARC-03 (bounded queue + cancellable job + owned process handle) can be built without touching
  `runtime/supervisor.py` at all — a `JobManager` can wrap `subprocess.Popen` directly in
  `demucs_adapter.py`/`history.py`, track the handle, and call `.terminate()`/`.kill()` on cancel or
  app close. Fully independent of ARC-04.
- ARC-04's "wire it in" option is not independent of ARC-03 — it would effectively become the ARC-03
  mechanism (the review's own framing: "a supervised worker IS a cancellable, bounded job"). But wiring
  it in requires far more than flipping a switch:
  1. Write a new production `Process` class (the protocol only has a test fake).
  2. Teach `demucs_worker.py` to emit protocol-v1 framed `hello`/periodic `progress`-or-`heartbeat`/one
     terminal frame on a stream `Supervisor` can read — Demucs' CLI (`demucs.separate.main`) has no
     built-in JSON-frame progress emission; either patch around Demucs' internal progress hooks or
     accept that the 30s `silence_timeout` will fire spuriously during genuine multi-minute inference
     unless heartbeats are added independently of Demucs progress.
  3. Stop combining stdout+stderr (`run_demucs` currently sets `stderr=subprocess.STDOUT`) — a framed
     binary protocol on the same stream as Demucs' own text logging is corrupted by design; diagnostics
     capture (`_diagnostic_tail`) would need to move to the separated stderr stream.
  4. `_cancelled`-style graceful cooperation from the worker is not strictly required —
     `Supervisor.run()`'s grace/escalation to `terminate()`/`kill()` works even if the worker never
     reads/acts on the `cancel` frame — but the worker still must emit a valid hello + not go silent
     for 30s, which it does not do today.
- So: **ARC-03 can ship alone. ARC-04's "wire it in" branch, if chosen, subsumes and satisfies ARC-03
  but is a materially larger, riskier build** (new IPC layer, new production Process implementation,
  worker rewrite, its own new tests) than a standalone lightweight `JobManager`. ARC-04's "remove it"
  branch is fully independent of ARC-03 and is nearly free given confirmed non-use.

## Approaches

### 1. Lightweight `JobManager` (ARC-03) + remove dormant supervisor/protocol code (ARC-04 option b)

One queue (`queue.Queue` or a simple list + condition), configurable concurrency (default 1), owns a
`Popen` per job directly in `demucs_adapter.py`/`guitar_adapter.py`, tracks it for
`.terminate()`→grace→`.kill()` on cancel or app close; delete `runtime/supervisor.py`,
`runtime/worker_main.py`, `runtime/__init__.py` and `Tests/Portable/test_worker_runtime.py` (keep
`protocol/framing.py`, `protocol/schemas.py`, `protocol/selftest.py` — the parent review confirms those
are not dead, referenced by test docs/utilities).

- **Pros**: independent of Demucs' CLI internals; no new IPC/framing to build; reuses the exact
  escalation shape (`terminate` → grace → `kill`) already proven in `Supervisor.run`, just without the
  protocol-framing overhead; smallest, most auditable diff; matches this project's existing "no
  framework merely to reorganize" bias (QLT-05's stated principle).
- **Cons**: throws away tested code (`Supervisor`, its escalation timing, its protocol validation) that
  the review explicitly said not to leave orphaned as an unused "security boundary that exists only in
  tests" — removal, not reuse, is the resolution; loses per-frame progress semantics `hello`/`heartbeat`
  was designed to carry (though nothing currently surfaces those to the GUI anyway).
- **Effort**: Medium.

### 2. Wire `Supervisor` into production (ARC-04 option a), let it double as the ARC-03 mechanism

Build a `Popen`-backed `Process` implementation; rewrite `demucs_worker.py` (or wrap it) to speak
protocol v1 (hello, periodic heartbeat during inference, exactly one terminal frame) on a stream
separated from Demucs' own stderr diagnostics; replace `run_demucs`'s blocking call with
`Supervisor.run_job(...)`; add a `JobManager` on top for the queue/concurrency-1 semantics ARC-03 also
needs (Supervisor itself has no queue — it runs one job at a time by construction, so some queueing
wrapper is still needed either way).

- **Pros**: the tested timeout/cancellation-escalation/protocol-validation logic gets a real caller
  (directly resolves ARC-04's "don't ship code that only tests exercise"); no dead-code cleanup debate;
  reuses 30s hello/silence timeouts as free hang detection.
- **Cons**: substantial new surface — a new `Process` class needs its own tests (the current test
  double doesn't count as production coverage); a worker rewrite touching how Demucs is invoked and how
  its progress/diagnostics are captured; risk of spurious `worker.hung` failures on legitimate
  multi-minute separations unless heartbeat emission is engineered inside the worker independently of
  Demucs' own progress reporting; changes the frozen PyInstaller build's process I/O contract (stdout
  framing) in a way that needs the same kind of apply-time verification Phase 1's C.1/C.2 needed for
  `--repo` wiring.
- **Effort**: High.

## Recommendation

Approach 1 (lightweight `JobManager` + remove the dormant supervisor/worker_main). The concrete
evidence gathered — no production `Process` implementation exists anywhere, `demucs_worker.py` speaks
zero protocol, `run_demucs` combines stdout/stderr in a way incompatible with framing, and
`worker_main.py`'s in-process network-denial model contradicts the actual out-of-process worker
topology — makes "wire it in" a materially larger and riskier lift than the review's phrasing ("likely
synergy") suggests. A `JobManager` built directly on `Popen`, reusing only the shape of `Supervisor`'s
terminate→grace→kill escalation (not its protocol-framing machinery), satisfies ARC-03 fully and
resolves ARC-04 by removing code that would otherwise keep claiming an untested-in-production boundary.
This should still be validated in the proposal/design phase against product intent — if there's a real
near-term plan for a multi-process/sandboxed worker model, Approach 2's investment is more justifiable
and shouldn't be foreclosed here.

## Open Questions / Tradeoffs for the Proposal Phase

- **Is concurrency-1 the right default?** Single-user CPU/GPU-bound desktop app; a strict FIFO queue of
  1 avoids the exhaustion the review flags, but rapid-add UX (user drags in 5 songs) means 4 sit
  "queued" with no current UI concept of a queue/pending state (`LibraryState`/`TrackRecord` status
  enum would need a new `"queued"` value distinct from `"preparing"`/`"processing"`). Confirm whether
  the product wants visible queueing or just a soft cap with rejection.
- **What does "cancel mid-inference" actually require?** No cooperative in-worker cancellation path
  exists today, and Demucs' CLI/library offers no clean interrupt point mid-`apply_model`. Realistic
  cancel semantics are "kill the subprocess" (Windows `subprocess.terminate()` → `TerminateProcess`, no
  SIGTERM equivalent to catch), same as `Supervisor.run()` already assumes via its terminate→kill
  escalation — state this explicitly in the design rather than implying a graceful stop.
- **Frozen-build process model verification**: any approach that changes how the GUI
  launches/reads/writes to `StemslayerWorker.exe` needs the same kind of apply-time PyInstaller
  verification Phase 1's C.1/C.2 did for `--repo` (confirm the frozen worker still resolves via
  `frozen_worker_path()`, and if stdout/stderr separation changes, confirm `CREATE_NO_WINDOW` + pipe
  behavior still works under the frozen console-subprocess setup).
- **`guitar_adapter.py` consistency**: it has an independent, structurally identical
  blocking-`subprocess.run` + `_cancelled`-token pattern (`run_specialist`). A `JobManager`/process-
  ownership fix scoped only to `demucs_adapter.py` would leave the Metal/Lead-Rhythm specialist path
  with the exact same unbounded/uncancellable behavior — scope should explicitly decide whether both
  adapters move together.
- **Dead-code removal scope**: beyond `runtime/worker_main.py:39-59`'s `run_worker` (the review's
  confirmed item), removal (if chosen) should also cover `runtime/supervisor.py`, `runtime/__init__.py`,
  and `Tests/Portable/test_worker_runtime.py` as a unit — leaving `Supervisor` itself while removing
  only `run_worker` would still leave an untested-in-production class. `protocol/framing.py`,
  `protocol/schemas.py`, `protocol/selftest.py` are confirmed not dead by the parent review (referenced
  by test docs/harness) and should not be touched.

## Risks

- No production `Process`/IPC implementation exists at all — Approach 2 has zero head start beyond the
  test double; risk of significant scope creep if selected without re-scoping as its own change.
- Cancellation today doesn't even reach `publish_atomic` in practice (no caller ever constructs a
  `CancellationToken`) — any fix must audit that this is genuinely wired end-to-end, not just
  parameter-plumbed like the current state.
- `guitar_adapter.py`'s parallel blocking-subprocess path could be silently left inconsistent if scope
  isn't explicit.
- Frozen-build process I/O changes (if Approach 2 or any stdout/stderr separation is chosen) carry the
  same class of risk Phase 1 flagged for apply-time verification (untested-until-build packaging
  assumptions).

## Ready for Proposal

Yes — the current-state mapping, coupling analysis, and concrete tradeoffs above are sufficient to
write a proposal. Recommend Approach 1 (lightweight JobManager + remove dormant supervisor) as the
working assumption for the proposal, but flag the concurrency-default and cancel-semantics questions
for explicit resolution in that phase rather than deciding them here.

## Key Learnings

1. `SeparationWorker/history.py` and `gui.py` never construct a `CancellationToken`, so today's
   cancellation mechanism in `publish_atomic` is unreachable dead capability, not a partial fix as the
   parent review's evidence implied.
2. The only `Process` protocol implementation for `runtime/supervisor.py`'s `Supervisor.run()` is a test
   fake (`ScriptedProcess`); no production `Popen`-backed implementation exists anywhere in the repository.
3. `demucs_worker.py`, the actual frozen worker entrypoint, forwards argv straight into
   `demucs.separate.main()` and speaks no protocol frames at all, while `run_demucs()` combines
   stdout/stderr in a way incompatible with JSON-length-prefixed framing.
4. `runtime/worker_main.py`'s `install_network_denial()` patches sockets in the current process, which
   only fits an in-process worker model — a structural mismatch with the actual out-of-process
   `StemslayerWorker.exe` architecture that ships today.
5. ARC-03 (bounded/cancellable jobs) can be resolved independently of ARC-04's supervisor-wiring branch;
   only the "wire it in" resolution of ARC-04 becomes coupled to ARC-03, and it is the materially larger
   of the two ARC-04 options.
