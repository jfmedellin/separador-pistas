# Instance Exclusivity Specification

## Purpose

Exactly one running instance of the application MUST own `HistoryStore` and history recovery at a time. This closes ARC-01: today every launch runs `recover_unfinished()` unconditionally, so a second instance corrupts the first's live `preparing`/`processing` jobs.

## ADDED Requirements

### Requirement: Single-Instance Lock Acquired Before History Access

The system MUST acquire an OS-level exclusive lock in `main()` before constructing `HistoryStore()` or calling `recover_unfinished()`.

#### Scenario: Normal single-instance startup proceeds unchanged

- GIVEN no other instance of the application is running
- WHEN the application starts
- THEN the system acquires the exclusive lock successfully
- AND `HistoryStore()` is constructed and `recover_unfinished()` runs exactly as today, flipping any stale `preparing`/`processing` rows to `interrupted`

### Requirement: Second Instance Exits Without Touching History

The system MUST detect that the exclusive lock is already held, show a message to the user, and exit without constructing `HistoryStore` or calling `recover_unfinished()`.

#### Scenario: Second instance launch while lock is held

- GIVEN a first instance is running and holds the exclusive lock
- WHEN a second instance is launched
- THEN the second instance MUST NOT construct `HistoryStore`
- AND the second instance MUST NOT call `recover_unfinished()`
- AND no `preparing`/`processing` row owned by the first instance is altered
- AND the second instance displays a message to the user and exits

### Requirement: Automatic Lock Release on Process Termination

The lock MUST be OS-managed so it releases automatically when the owning process terminates, including an abnormal termination (crash or hard kill), never relying on a presence-only sentinel file.

#### Scenario: Lock releases on normal exit

- GIVEN the running instance holds the exclusive lock
- WHEN that instance exits normally
- THEN the lock is released
- AND a subsequently launched instance can acquire the lock and run `recover_unfinished()`

#### Scenario: Lock releases after a crash, allowing later relaunch

- GIVEN the running instance holds the exclusive lock
- WHEN that instance terminates abnormally (crash or forced kill) without an orderly shutdown
- THEN the OS releases the lock as part of process teardown
- AND a later relaunch of the application acquires the lock successfully and is never permanently blocked
