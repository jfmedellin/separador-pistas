# Metal Lead/Rhythm Specialist — Admission Contract

## Status

**No specialist is admitted.** `SeparationWorker.guitar_adapter.REGISTERED_SPECIALISTS`
is empty and the Metal profile is disabled.

Admission reviewed the publicly available candidates and rejected all of them.
None satisfies semantic Lead/Rhythm decomposition together with redistributable
weights and offline Windows operation:

| Candidate | Why it was rejected |
|---|---|
| `htdemucs_6s` | Extracts combined guitar only; no role decomposition. Retained as the primary guitar-isolation stage. |
| Banquet / query-bandit | Separates guitar *classes* (distorted, clean, acoustic), not roles. Weights are CC BY-NC-SA 4.0. |
| GuitarDuets adapted HTDemucs | Permutation-invariant Guitar 1/Guitar 2 on classical guitar, not stable roles. No published inference repository or checkpoint. |
| LeadInstrumentDetection | Classifies lead segments; produces no separated audio. |
| Open-Unmix guitar fork | Guitar versus accompaniment. No published guitar checkpoint. |

This document is therefore not a review of an existing model. It is the
contract that any future specialist must satisfy before it can be registered.

## What a candidate must supply

### 1. Identity and provenance

| Field | Requirement |
|---|---|
| `specialist_id` | Stable, unique, never reused across weights |
| `entrypoint` | Importable module invoked as `python -m <entrypoint>` |
| `model_file` | Path relative to the asset root; no symlink, no parent segments |
| `sha256` | Digest of the exact registered bytes |
| `version` | Immutable version of the trained weights |
| `origin` | Where the bytes came from, with a resolvable reference |
| `author` | Who trained the weights |

Ambiguous, unsourced, or self-asserted provenance is rejected.

### 2. Rights

- License identifier, evidence, and full license text.
- Redistribution rights for the weight bytes, stated explicitly.
- Training-data provenance and the rights under which that data was used.

A model trained on material whose rights do not permit the resulting weights
to be redistributed is rejected regardless of its measured quality.

### 3. Runtime

- Loads and infers offline on Windows under the production runtime.
- Makes no network request during inference. A candidate declaring
  `network_required` is rejected at resolution time.
- Runs through the adapter's argv-only, `shell=False` boundary with a trusted
  `sys.executable` and a registered entrypoint.
- Honors cancellation and produces no partial published output.
- Accepts 44.1 kHz stereo WAV input and emits `lead_guitar.wav` and
  `rhythm_guitar.wav`, aligned in frame zero, rate, channels, and duration.

### 4. Semantics

The candidate must implement the role assignment contract, not a permutation:

- `Lead Guitar` carries foreground melodic and solo material.
- `Rhythm Guitar` carries harmonic accompaniment and riff material, including
  passages where it is the only guitar sounding.
- Roles are stable across a track. A permutation-invariant Guitar 1/Guitar 2
  model does not satisfy this contract, because lane identity would not be
  reproducible between runs or between tracks.

### 5. Role absence

Lead guitar is intermittent by construction in metal arrangements. A candidate
must handle absence correctly:

- A silent `Lead Guitar` lane is a valid result whenever
  `lead + rhythm` reconstructs the isolated guitar family within the calibrated
  limit. It publishes with a declared manifest reason.
- A silent lane whose reconstruction fails is lost energy and must fail closed.
- Absence is never judged by lane energy alone.

### 6. Quality evidence

Measured on a rights-cleared dense metal corpus, recorded per fixture:

| Gate | What it proves |
|---|---|
| Alignment | Frame zero, sample rate, channel count, and duration match the input |
| Audibility | Non-absent lanes carry meaningful signal, not near-silence |
| Reconstruction | `lead + rhythm` reproduces the isolated guitar family |
| Leakage | Role lanes do not duplicate each other's energy |
| Role absence | Tracks with no lead publish a silent lead lane and still reconstruct |
| Perceptual | Blinded listening on dense metal material, recorded with method and raters |

Thresholds live in `thresholds.json`. They are **uncalibrated targets** until a
rights-cleared corpus exists; admission cannot pass while they carry
`"calibrated": false`.

## Recommended path

No admissible model exists, so the specialist has to be built. The recommended
architecture is a role-aware two-output HTDemucs-family checkpoint applied to
the guitar family already isolated by `htdemucs_6s`.

The blocking problem is the corpus, not the architecture. Public metal
multitracks with lead/rhythm labels and usable rights do not exist; MoisesDB
carries track-level `lead guitar` labels but is CC BY-NC-SA 4.0 and is not
metal-focused.

The viable corpus is synthetic and rights-owned: render or record guitar DI
signals, apply amplifier simulation per track, and mix lead and rhythm sources
into training mixtures. Role labels are then exact by construction and need no
annotation, and no training source ever carries both roles. GuitarDuets
validated this synthesis approach, though on classical guitar and with
permutation labels rather than roles.

Training happens in a separate repository with its own runtime. Only the
resulting checkpoint, its evidence, and its registration enter this project.

## Registration

A candidate is registered only after `Tools/admit_metal_guitar_model.py`
reports `ADMITTED` with every gate passing against calibrated thresholds and
recorded perceptual evidence. Until then the Metal profile stays disabled and
the legacy four-stem profile remains available.
