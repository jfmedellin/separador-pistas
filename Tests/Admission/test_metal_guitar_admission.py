import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np

from SeparationWorker.guitar_adapter import SpecialistEntry

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPOSITORY_ROOT / "Tools" / "admit_metal_guitar_model.py"
SHIPPED_THRESHOLDS = REPOSITORY_ROOT / "Compliance" / "evidence" / "metal-guitar" / "thresholds.json"

_spec = importlib.util.spec_from_file_location("admit_metal_guitar_model", TOOL_PATH)
admit = importlib.util.module_from_spec(_spec)
# Register before executing: dataclasses resolves annotations through sys.modules.
sys.modules[_spec.name] = admit
_spec.loader.exec_module(admit)

SAMPLE_RATE = 44100
SPECIALIST_ID = "metal-lead-rhythm-v1"


def tone(frequency, amplitude=0.5, seconds=1.0):
    """Return one stereo tone whose period divides the window exactly."""
    frames = int(SAMPLE_RATE * seconds)
    time = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    wave = amplitude * np.sin(2.0 * np.pi * frequency * time)
    return np.stack([wave, wave], axis=1)


def calibrated_thresholds():
    thresholds = json.loads(SHIPPED_THRESHOLDS.read_text(encoding="utf-8"))
    thresholds["calibrated"] = True
    return thresholds


def complete_perceptual_evidence():
    return {
        "method": "blinded ABX on dense metal excerpts",
        "raters": 6,
        "preference_ratio": 0.83,
        "material": "rights-cleared internal metal corpus",
        "recorded_at": "2026-08-29",
    }


class ShippedStateTests(unittest.TestCase):
    def test_shipped_thresholds_are_marked_uncalibrated(self):
        thresholds = json.loads(SHIPPED_THRESHOLDS.read_text(encoding="utf-8"))

        self.assertIs(False, thresholds["calibrated"])
        self.assertTrue(thresholds["calibration_blocker"])

    def test_shipped_state_denies_admission(self):
        report = admit.evaluate(
            thresholds=json.loads(SHIPPED_THRESHOLDS.read_text(encoding="utf-8")),
            specialist_id=None,
            asset_root=REPOSITORY_ROOT / "Compliance" / "assets",
            fixtures=None,
        )

        self.assertFalse(report.admitted)
        self.assertIn("DENIED", report.render())
        self.assertEqual(
            ["thresholds", "specialist", "fixtures", "perceptual"],
            [gate.name for gate in report.gates],
        )
        self.assertTrue(all(not gate.passed for gate in report.gates))

    def test_uncalibrated_thresholds_fail_closed_even_with_every_other_gate_ready(self):
        with tempfile.TemporaryDirectory() as root:
            fixtures = Path(root) / "fixtures"
            fixtures.mkdir()
            (fixtures / "PROVENANCE.json").write_text("{}", encoding="utf-8")

            report = admit.evaluate(
                thresholds=json.loads(SHIPPED_THRESHOLDS.read_text(encoding="utf-8")),
                specialist_id=None,
                asset_root=Path(root),
                fixtures=fixtures,
                perceptual_evidence=complete_perceptual_evidence(),
            )

        self.assertFalse(report.admitted)
        thresholds_gate = next(gate for gate in report.gates if gate.name == "thresholds")
        self.assertFalse(thresholds_gate.passed)

    def test_every_gate_passing_admits_the_specialist(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root = Path(root) / "assets"
            model = asset_root / "metal" / "lead_rhythm.pt"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"weights")
            registry = {
                SPECIALIST_ID: SpecialistEntry(
                    SPECIALIST_ID,
                    "metal_specialist.separate",
                    "metal/lead_rhythm.pt",
                    hashlib.sha256(b"weights").hexdigest(),
                )
            }
            fixtures = Path(root) / "fixtures"
            fixtures.mkdir()
            (fixtures / "PROVENANCE.json").write_text("{}", encoding="utf-8")

            report = admit.evaluate(
                thresholds=calibrated_thresholds(),
                specialist_id=SPECIALIST_ID,
                asset_root=asset_root,
                fixtures=fixtures,
                perceptual_evidence=complete_perceptual_evidence(),
                registry=registry,
            )

        self.assertTrue(report.admitted)
        self.assertIn("ADMITTED", report.render())


class EvidenceGateTests(unittest.TestCase):
    def test_fixtures_without_provenance_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixtures = Path(root) / "fixtures"
            fixtures.mkdir()

            gate = admit.check_fixtures(fixtures)

        self.assertFalse(gate.passed)
        self.assertIn("PROVENANCE.json", gate.detail)

    def test_incomplete_perceptual_evidence_is_rejected(self):
        evidence = complete_perceptual_evidence()
        del evidence["raters"]

        gate = admit.check_perceptual_evidence(evidence)

        self.assertFalse(gate.passed)
        self.assertIn("raters", gate.detail)

    def test_absent_perceptual_evidence_is_rejected(self):
        self.assertFalse(admit.check_perceptual_evidence(None).passed)


class AudioMetricTests(unittest.TestCase):
    def test_digital_silence_reports_negative_infinity(self):
        self.assertEqual(-np.inf, admit.peak_dbfs(np.zeros((128, 2))))

    def test_peak_level_matches_amplitude(self):
        self.assertAlmostEqual(-6.0, admit.peak_dbfs(tone(440.0, amplitude=0.5)), places=1)

    def test_exact_reconstruction_is_infinite(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)

        self.assertEqual(np.inf, admit.signal_to_residual_db(lead + rhythm, lead + rhythm))

    def test_dropped_role_collapses_reconstruction(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)

        measured = admit.signal_to_residual_db(lead + rhythm, rhythm)

        self.assertLess(measured, 10.0)

    def test_orthogonal_roles_report_low_leakage(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)

        self.assertLess(admit.cross_role_energy_ratio_db(lead, rhythm), -12.0)

    def test_duplicated_role_energy_reports_high_leakage(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)

        measured = admit.cross_role_energy_ratio_db(lead + 0.5 * rhythm, rhythm)

        self.assertGreater(measured, -12.0)


class FixtureGateTests(unittest.TestCase):
    def gates(self, combined, lead, rhythm, **overrides):
        return {
            gate.name: gate
            for gate in admit.evaluate_fixture(combined, lead, rhythm, calibrated_thresholds(), **overrides)
        }

    def test_clean_decomposition_passes_every_gate(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)

        gates = self.gates(
            lead + rhythm, lead, rhythm, lead_reference=lead, rhythm_reference=rhythm
        )

        self.assertTrue(all(gate.passed for gate in gates.values()), gates)
        self.assertIn("fixture/lead-audibility", gates)
        self.assertIn("fixture/lead-leakage", gates)

    def test_misaligned_lanes_fail_before_any_other_gate(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4, seconds=0.5)

        gates = self.gates(lead, lead, rhythm)

        self.assertEqual(["fixture/alignment"], list(gates))
        self.assertFalse(gates["fixture/alignment"].passed)

    def test_track_without_lead_publishes_when_reconstruction_holds(self):
        rhythm = tone(110.0, amplitude=0.4)
        silent_lead = np.zeros_like(rhythm)

        gates = self.gates(rhythm, silent_lead, rhythm)

        self.assertTrue(gates["fixture/reconstruction"].passed)
        self.assertTrue(gates["fixture/lead-absence"].passed)
        self.assertIn("the role is absent", gates["fixture/lead-absence"].detail)
        self.assertNotIn("fixture/lead-audibility", gates)

    def test_silent_lead_with_lost_energy_fails_closed(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)
        silent_lead = np.zeros_like(lead)

        gates = self.gates(lead + rhythm, silent_lead, rhythm)

        self.assertFalse(gates["fixture/reconstruction"].passed)
        self.assertFalse(gates["fixture/lead-absence"].passed)
        self.assertIn("lost energy", gates["fixture/lead-absence"].detail)

    def test_noise_floor_placeholder_is_neither_absent_nor_audible(self):
        rhythm = tone(110.0, amplitude=0.4)
        placeholder_lead = tone(880.0, amplitude=1e-3)

        gates = self.gates(placeholder_lead + rhythm, placeholder_lead, rhythm)

        self.assertTrue(gates["fixture/reconstruction"].passed)
        self.assertNotIn("fixture/lead-absence", gates)
        self.assertFalse(gates["fixture/lead-audibility"].passed)

    def test_duplicated_guitar_energy_fails_the_leakage_gate(self):
        lead, rhythm = tone(880.0), tone(110.0, amplitude=0.4)

        gates = self.gates(
            lead + rhythm,
            lead + 0.5 * rhythm,
            0.5 * rhythm,
            lead_reference=lead,
            rhythm_reference=rhythm,
        )

        self.assertFalse(gates["fixture/lead-leakage"].passed)


class CommandLineTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = admit.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_admission_refuses_to_run_without_the_offline_assertion(self):
        code, _, error = self.run_main([])

        self.assertEqual(2, code)
        self.assertIn("--offline", error)

    def test_shipped_offline_run_reports_denied(self):
        code, output, _ = self.run_main(["--offline"])

        self.assertEqual(1, code)
        self.assertIn("DENIED", output)
        self.assertIn("thresholds", output)

    def test_unregistered_specialist_is_reported_actionably(self):
        code, output, _ = self.run_main(["--offline", "--specialist", SPECIALIST_ID])

        self.assertEqual(1, code)
        self.assertIn("specialist.unregistered", output)


if __name__ == "__main__":
    unittest.main()
