# Source Identity Immutability Specification

## Purpose

A job's stems MUST bind to the exact bytes that were hashed for that job. This closes ARC-02: today `_prepare()` hashes the live, mutable user file and then separates that same mutable path, so editing the file between those two steps binds new-content stems to a stale identity claim.

## ADDED Requirements

### Requirement: Immutable Copy Created Before Hashing

The system MUST copy the source file into an app-owned, job-scoped location at the top of `_prepare()`, before any hashing occurs.

#### Scenario: Normal add flow copies source before hashing

- GIVEN a user adds a source file to a job
- WHEN `_prepare()` runs for that job
- THEN the system copies the source file to an immutable, job-owned path first
- AND only then computes the content hash used for dedup/identity

### Requirement: Hashing and Separation Read Only the Immutable Copy

The system MUST read only the job-owned immutable copy for both hashing and separation, and MUST NOT re-read `record.source_path` after the copy step completes.

#### Scenario: Hash is computed from the immutable copy

- GIVEN the immutable copy has been created for a job
- WHEN the system computes the job's content hash
- THEN the hash input is the immutable copy's bytes, not `record.source_path`

#### Scenario: Separation reads the immutable copy

- GIVEN the immutable copy has been created and hashed for a job
- WHEN the Demucs subprocess runs separation for that job
- THEN the input path passed to separation is the immutable copy, not `record.source_path`

### Requirement: Copy Deleted on Terminal State

The system MUST delete the job-owned immutable copy once the job reaches a terminal state (`ready` or `failed`), using the existing removal path (`history.py:445-461`), and MUST NOT retain it for later re-splits.

#### Scenario: Copy is deleted when a job completes successfully

- GIVEN a job's immutable copy exists on disk
- WHEN the job transitions to the `ready` terminal state
- THEN the system deletes the immutable copy via the existing removal path

#### Scenario: Copy is deleted when a job fails

- GIVEN a job's immutable copy exists on disk
- WHEN the job transitions to the `failed` terminal state
- THEN the system deletes the immutable copy via the existing removal path

### Requirement: Post-Add Source Mutation Cannot Alter Job Identity

Mutating the original source file after `add()` returns MUST NOT be able to change what gets hashed or separated for that job.

#### Scenario: Editing the source file after add() does not affect the job

- GIVEN a job's immutable copy has already been created from the source file
- WHEN the original source file is modified or replaced on disk after `add()` returns
- THEN the job's hash and separation input remain the untouched immutable copy
- AND no stem produced by that job reflects the post-add mutation
