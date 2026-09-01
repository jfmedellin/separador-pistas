# Model Integrity Specification

## Purpose

Demucs model weights MUST be acquired, verified, and loaded entirely under app control, with no code path able to reach `RemoteRepo`/`torch.hub.load_state_dict_from_url`. This closes SEC-01: a compromised mirror serving unverified weights currently allows arbitrary code execution on first run.

## ADDED Requirements

### Requirement: Verified First-Use Model Acquisition

The system MUST download a model profile's weight/config files on first use into an app-owned directory, verify each file against a full SHA-256 manifest entry, and only then invoke Demucs via `--repo <verified-dir>`.

#### Scenario: Successful first-use acquisition and verified invocation

- GIVEN a model profile (e.g. `htdemucs`) has never been acquired locally
- WHEN separation starts for that profile
- THEN the system downloads the profile's files, computes SHA-256 for each, and compares against the pinned manifest
- AND all files match
- AND the Demucs subprocess is invoked with `--repo <verified-dir>` pointing at the app-owned `LocalRepo`-shaped directory
- AND no `torch.hub` or `RemoteRepo` code path executes

### Requirement: Fail-Closed Hash Verification

The system MUST fail the run closed on any SHA-256 mismatch or fetch failure, with no fallback to `RemoteRepo`/`torch.hub` and no user override.

#### Scenario: Hash mismatch blocks separation entirely

- GIVEN a downloaded model file's computed SHA-256 does not match the pinned manifest entry
- WHEN the system finishes verification
- THEN separation for that job MUST NOT start
- AND the system MUST NOT fall back to `RemoteRepo` or `torch.hub.load_state_dict_from_url`
- AND the job surfaces a verification-failure error, not a silent retry with unverified data

#### Scenario: Fetch failure blocks separation entirely

- GIVEN the download of a model file fails or is incomplete
- WHEN acquisition finishes
- THEN separation for that job MUST NOT start
- AND no partially-downloaded or unverified file is passed to `--repo`

### Requirement: Independent Per-Profile Acquisition

The system MUST acquire and verify each model profile independently, so that one profile's presence or absence does not affect another's.

#### Scenario: Second profile acquires independently of the first

- GIVEN `htdemucs` is already acquired and verified locally
- WHEN separation is requested with the `htdemucs_6s` profile for the first time
- THEN the system acquires and verifies `htdemucs_6s`'s own manifest entries
- AND the previously verified `htdemucs` files are left untouched
- AND both profiles are independently invocable via their own `--repo <verified-dir>`

### Requirement: Reuse of Already-Verified Local Model

The system MUST reuse a previously verified, on-disk model profile without re-downloading or re-fetching from the network on a later run.

#### Scenario: Later run reuses verified model without network access

- GIVEN a model profile was acquired and verified in a prior run
- WHEN a later run requests separation with that same profile
- THEN the system MUST NOT initiate a new download for that profile's files
- AND the Demucs subprocess is invoked with `--repo <verified-dir>` using the already-verified local files
