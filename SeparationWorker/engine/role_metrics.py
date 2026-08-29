"""The audio measurements that decide whether a role decomposition is valid.

Admission and the runtime pipeline must agree exactly on what "absent" and
"reconstructed" mean. If they drifted apart, a checkpoint could pass admission
and still publish results the pipeline rejects, or worse, the other way round.
Both therefore call the functions defined here and nothing else.

The rule these encode: role absence is decided by reconstruction, never by
lane energy alone. A silent lane whose pair still reconstructs the source is a
correct result; a silent lane that loses energy is a failure.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SILENCE_FLOOR = 1e-12
THRESHOLDS_FILE = "thresholds.json"


@dataclass(frozen=True)
class RoleThresholds:
    """Calibrated limits governing role decomposition."""

    calibrated: bool
    reconstruction_minimum_db: float
    leakage_maximum_db: float
    audibility_minimum_dbfs: float
    absence_at_or_below_dbfs: float
    calibration_blocker: str = ""

    @classmethod
    def from_payload(cls, payload: dict) -> "RoleThresholds":
        gates = payload["gates"]
        return cls(
            calibrated=payload.get("calibrated") is True,
            reconstruction_minimum_db=float(gates["reconstruction"]["minimum_db"]),
            leakage_maximum_db=float(gates["leakage"]["maximum_db"]),
            audibility_minimum_dbfs=float(gates["audibility"]["minimum_dbfs"]),
            absence_at_or_below_dbfs=float(gates["role_absence"]["absent_at_or_below_dbfs"]),
            calibration_blocker=str(payload.get("calibration_blocker", "")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "RoleThresholds":
        return cls.from_payload(json.loads(Path(path).read_text(encoding="utf-8")))


# A deterministic split has no weights and no learned behaviour, so there is
# nothing a rights-cleared corpus could calibrate about it: its reconstruction
# is a property of the arithmetic, not of training. Absence and audibility
# still need limits, and these are the same ones the trained path targets.
DETERMINISTIC_ROLE_THRESHOLDS = RoleThresholds(
    calibrated=True,
    reconstruction_minimum_db=60.0,
    leakage_maximum_db=0.0,
    audibility_minimum_dbfs=-40.0,
    absence_at_or_below_dbfs=-80.0,
    calibration_blocker="",
)


def peak_dbfs(samples) -> float:
    """Return the peak level in dBFS, or negative infinity for digital silence."""
    samples = np.asarray(samples, dtype=np.float64)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak <= SILENCE_FLOOR:
        return -math.inf
    return 20.0 * math.log10(peak)


def signal_to_residual_db(reference, estimate) -> float:
    """Return how far the estimate reproduces the reference, in dB."""
    reference = np.asarray(reference, dtype=np.float64)
    estimate = np.asarray(estimate, dtype=np.float64)
    if reference.shape != estimate.shape:
        raise ValueError("Reconstruction requires identically shaped signals.")
    reference_energy = float(np.sum(reference**2))
    residual_energy = float(np.sum((reference - estimate) ** 2))
    if reference_energy <= SILENCE_FLOOR:
        return math.inf if residual_energy <= SILENCE_FLOOR else -math.inf
    if residual_energy <= SILENCE_FLOOR:
        return math.inf
    return 10.0 * math.log10(reference_energy / residual_energy)


def cross_role_energy_ratio_db(lane, other_reference) -> float:
    """Return how much of the other role's reference energy appears in this lane."""
    lane = np.asarray(lane, dtype=np.float64)
    other_reference = np.asarray(other_reference, dtype=np.float64)
    if lane.shape != other_reference.shape:
        raise ValueError("Leakage requires identically shaped signals.")
    other_energy = float(np.sum(other_reference**2))
    lane_energy = float(np.sum(lane**2))
    if other_energy <= SILENCE_FLOOR or lane_energy <= SILENCE_FLOOR:
        return -math.inf
    projection = float(np.sum(lane * other_reference)) / other_energy
    leaked_energy = (projection**2) * other_energy
    if leaked_energy <= SILENCE_FLOOR:
        return -math.inf
    return 10.0 * math.log10(leaked_energy / lane_energy)


def is_absent(samples, thresholds: RoleThresholds) -> bool:
    """Report whether a role lane is silent enough to count as an absent role."""
    return peak_dbfs(samples) <= thresholds.absence_at_or_below_dbfs


def is_audible(samples, thresholds: RoleThresholds) -> bool:
    """Report whether a lane carries meaningful signal rather than a noise floor."""
    return peak_dbfs(samples) >= thresholds.audibility_minimum_dbfs


def reconstructs(reference, estimate, thresholds: RoleThresholds) -> bool:
    """Report whether an estimate reproduces its reference within the limit."""
    return signal_to_residual_db(reference, estimate) >= thresholds.reconstruction_minimum_db
