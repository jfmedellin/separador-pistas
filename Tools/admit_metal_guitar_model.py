"""Offline admission harness for the Metal Lead/Rhythm guitar specialist.

Admission is deny-by-default. The harness never downloads, never trains, and
never enables the Metal profile on its own: it reports whether a registered
specialist satisfies every gate in
`Compliance/evidence/metal-guitar/CONTRACT.md`. With no registered specialist
it reports DENIED, which is the expected shipped state.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# Admission measures with the same functions the runtime pipeline enforces, so
# a checkpoint can never pass one definition of "absent" and fail the other.
from SeparationWorker.engine.role_metrics import (  # noqa: E402
    RoleThresholds,
    cross_role_energy_ratio_db,
    peak_dbfs,
    signal_to_residual_db,
)
from SeparationWorker.guitar_adapter import (  # noqa: E402
    GuitarSpecialistError,
    resolve_specialist,
)

DEFAULT_THRESHOLDS = REPOSITORY_ROOT / "Compliance" / "evidence" / "metal-guitar" / "thresholds.json"
DEFAULT_ASSET_ROOT = REPOSITORY_ROOT / "Compliance" / "assets"
REQUIRED_PERCEPTUAL_FIELDS = ("method", "raters", "preference_ratio", "material", "recorded_at")


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class AdmissionReport:
    specialist_id: str | None
    gates: tuple[GateResult, ...]

    @property
    def admitted(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates)

    def render(self) -> str:
        subject = self.specialist_id or "<no specialist>"
        lines = [f"Metal Lead/Rhythm admission for {subject}", ""]
        for gate in self.gates:
            lines.append(f"  [{'PASS' if gate.passed else 'FAIL'}] {gate.name}: {gate.detail}")
        lines.extend(["", "ADMITTED" if self.admitted else "DENIED"])
        return "\n".join(lines)


def role_is_absent(samples: np.ndarray, thresholds: dict) -> bool:
    """Report whether a role lane is silent enough to count as an absent role."""
    return peak_dbfs(samples) <= RoleThresholds.from_payload(thresholds).absence_at_or_below_dbfs


def check_thresholds(thresholds: dict) -> GateResult:
    if thresholds.get("calibrated") is not True:
        blocker = thresholds.get("calibration_blocker", "Thresholds are not calibrated.")
        return GateResult("thresholds", False, blocker)
    return GateResult("thresholds", True, "Calibrated thresholds are available.")


def check_specialist(specialist_id: str | None, asset_root: Path, registry=None) -> GateResult:
    if not specialist_id:
        return GateResult(
            "specialist",
            False,
            "No specialist identifier was requested; the registry ships empty and Metal stays disabled.",
        )
    try:
        resolved = resolve_specialist(specialist_id, asset_root, registry=registry)
    except GuitarSpecialistError as error:
        return GateResult("specialist", False, f"{error.code}: {error.cause}")
    return GateResult(
        "specialist",
        True,
        f"Resolved {resolved.specialist_id} at {resolved.model_path.name} ({resolved.sha256[:16]}...).",
    )


def check_fixtures(fixtures: Path | None) -> GateResult:
    if fixtures is None:
        return GateResult("fixtures", False, "No rights-cleared fixture corpus was supplied.")
    provenance = fixtures / "PROVENANCE.json"
    if not provenance.is_file():
        return GateResult(
            "fixtures",
            False,
            f"Fixture provenance is missing at {provenance}; rights must be proven per fixture.",
        )
    return GateResult("fixtures", True, f"Fixture provenance found at {provenance}.")


def check_perceptual_evidence(evidence: dict | None) -> GateResult:
    if not isinstance(evidence, dict):
        return GateResult("perceptual", False, "No blinded listening evidence was recorded.")
    missing = [name for name in REQUIRED_PERCEPTUAL_FIELDS if not evidence.get(name)]
    if missing:
        return GateResult("perceptual", False, f"Blinded listening evidence is incomplete: {', '.join(missing)}.")
    return GateResult("perceptual", True, "Blinded listening evidence is recorded.")


def evaluate_fixture(
    combined: np.ndarray,
    lead: np.ndarray,
    rhythm: np.ndarray,
    thresholds: dict,
    *,
    name: str = "fixture",
    lead_reference: np.ndarray | None = None,
    rhythm_reference: np.ndarray | None = None,
) -> tuple[GateResult, ...]:
    """Evaluate one decomposed fixture against the calibrated audio gates."""
    gates: list[GateResult] = []
    if not (combined.shape == lead.shape == rhythm.shape):
        return (
            GateResult(
                f"{name}/alignment",
                False,
                "Role lanes do not align with the isolated guitar family in shape.",
            ),
        )
    gates.append(GateResult(f"{name}/alignment", True, "Role lanes align with the guitar family."))

    limits = thresholds["gates"]
    reconstruction = signal_to_residual_db(combined, lead + rhythm)
    reconstructed = reconstruction >= limits["reconstruction"]["minimum_db"]
    gates.append(
        GateResult(
            f"{name}/reconstruction",
            reconstructed,
            f"lead + rhythm reproduces the guitar family at {reconstruction:.1f} dB "
            f"(minimum {limits['reconstruction']['minimum_db']:.1f} dB).",
        )
    )

    # Role absence is judged by reconstruction, never by lane energy alone: a
    # silent lead lane is correct when the pair still reconstructs, and is lost
    # energy when it does not.
    for role, samples in (("lead", lead), ("rhythm", rhythm)):
        absent = role_is_absent(samples, thresholds)
        if absent:
            gates.append(
                GateResult(
                    f"{name}/{role}-absence",
                    reconstructed,
                    (
                        f"{role} is silent and reconstruction holds; the role is absent in this fixture."
                        if reconstructed
                        else f"{role} is silent while reconstruction fails; the decomposition lost energy."
                    ),
                )
            )
            continue
        level = peak_dbfs(samples)
        audible = level >= limits["audibility"]["minimum_dbfs"]
        gates.append(
            GateResult(
                f"{name}/{role}-audibility",
                audible,
                f"{role} peaks at {level:.1f} dBFS "
                f"(minimum {limits['audibility']['minimum_dbfs']:.1f} dBFS).",
            )
        )

    references = {"lead": rhythm_reference, "rhythm": lead_reference}
    for role, samples in (("lead", lead), ("rhythm", rhythm)):
        other = references[role]
        if other is None:
            continue
        leakage = cross_role_energy_ratio_db(samples, other)
        gates.append(
            GateResult(
                f"{name}/{role}-leakage",
                leakage <= limits["leakage"]["maximum_db"],
                f"{role} carries {leakage:.1f} dB of the other role "
                f"(maximum {limits['leakage']['maximum_db']:.1f} dB).",
            )
        )
    return tuple(gates)


def evaluate(
    *,
    thresholds: dict,
    specialist_id: str | None,
    asset_root: Path,
    fixtures: Path | None,
    perceptual_evidence: dict | None = None,
    registry=None,
) -> AdmissionReport:
    """Run every admission gate that can be decided without a specialist run."""
    gates = (
        check_thresholds(thresholds),
        check_specialist(specialist_id, asset_root, registry=registry),
        check_fixtures(fixtures),
        check_perceptual_evidence(perceptual_evidence),
    )
    return AdmissionReport(specialist_id, gates)


def load_thresholds(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Admit or deny a Metal Lead/Rhythm guitar specialist.")
    parser.add_argument("--offline", action="store_true", help="Assert that admission runs without network access.")
    parser.add_argument("--fixtures", type=Path, default=None, help="Rights-cleared metal fixture corpus.")
    parser.add_argument("--specialist", default=None, help="Registered specialist identifier to admit.")
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT, help="Bundled asset root.")
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS, help="Calibrated threshold file.")
    arguments = parser.parse_args(argv)

    if not arguments.offline:
        print("Admission runs offline only. Re-run with --offline to assert that contract.", file=sys.stderr)
        return 2

    evidence_file = arguments.fixtures / "PERCEPTUAL.json" if arguments.fixtures else None
    perceptual_evidence = None
    if evidence_file is not None and evidence_file.is_file():
        perceptual_evidence = json.loads(evidence_file.read_text(encoding="utf-8"))

    report = evaluate(
        thresholds=load_thresholds(arguments.thresholds),
        specialist_id=arguments.specialist,
        asset_root=arguments.asset_root,
        fixtures=arguments.fixtures,
        perceptual_evidence=perceptual_evidence,
    )
    print(report.render())
    return 0 if report.admitted else 1


if __name__ == "__main__":
    raise SystemExit(main())
