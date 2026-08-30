import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataclasses import replace
from types import SimpleNamespace

from Compliance.registry import (
    ALLOWED_CAPABILITIES,
    ComplianceError,
    canonical_manifest_bytes,
    check_offline_readiness,
    check_profile_readiness,
    profile_capabilities,
    verify_manifest,
)
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE, METAL_PROFILE


def manifest_for(asset_path, **overrides):
    manifest = {
        "schema_version": 1,
        "asset_id": "four-stem-model",
        "kind": "model",
        "file": asset_path.name,
        "version": "1.0.0",
        "origin": "bundled-test-fixture",
        "sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
        "author": "Fixture Author",
        "license": {
            "identifier": "LicenseRef-Private-Test",
            "evidence": "Tests/Fixtures/LICENSE.txt",
            "text": "Private test fixture only.",
        },
        "capabilities": sorted(ALLOWED_CAPABILITIES),
        "private_non_commercial_authorized": True,
    }
    manifest.update(overrides)
    return manifest


def trust(manifest):
    return {manifest["asset_id"]: hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()}


class ComplianceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset = self.root / "model.bin"
        self.asset.write_bytes(b"approved fixture bytes")

    def tearDown(self):
        self.temporary.cleanup()

    def assert_compliance_error(self, code, operation, asset=None):
        with self.assertRaises(ComplianceError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(code, error.code)
        self.assertTrue(error.cause)
        self.assertTrue(error.recovery)
        if asset is not None:
            self.assertEqual(asset, error.asset)

    def test_accepts_registered_hash_valid_four_capability_manifest(self):
        manifest = manifest_for(self.asset)

        verified = verify_manifest(manifest, self.root, trust(manifest))

        self.assertEqual("four-stem-model", verified.asset_id)
        self.assertEqual(ALLOWED_CAPABILITIES, verified.capabilities)

    def test_rejects_forged_manifest(self):
        approved = manifest_for(self.asset)
        trusted = trust(approved)
        forged = dict(approved, author="Untrusted Author")

        self.assert_compliance_error(
            "manifest.forged",
            lambda: verify_manifest(forged, self.root, trusted),
            "four-stem-model",
        )

    def test_rejects_unregistered_manifest(self):
        manifest = manifest_for(self.asset)
        self.assert_compliance_error(
            "manifest.unregistered",
            lambda: verify_manifest(manifest, self.root, {}),
            "four-stem-model",
        )

    def test_rejects_missing_asset(self):
        manifest = manifest_for(self.asset)
        trusted = trust(manifest)
        self.asset.unlink()

        self.assert_compliance_error(
            "asset.missing",
            lambda: verify_manifest(manifest, self.root, trusted),
            "four-stem-model",
        )

    def test_rejects_hash_invalid_asset(self):
        manifest = manifest_for(self.asset)
        trusted = trust(manifest)
        self.asset.write_bytes(b"tampered")

        self.assert_compliance_error(
            "asset.hash_mismatch",
            lambda: verify_manifest(manifest, self.root, trusted),
            "four-stem-model",
        )

    def test_wraps_asset_read_failure_actionably(self):
        manifest = manifest_for(self.asset)
        with patch.object(Path, "open", side_effect=OSError("deterministic read failure")):
            with self.assertRaises(ComplianceError) as caught:
                verify_manifest(manifest, self.root, trust(manifest))
        self.assertEqual("asset.read_failed", caught.exception.code)
        self.assertEqual("four-stem-model", caught.exception.asset)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(caught.exception.recovery)

    def test_rejects_path_escape(self):
        manifest = manifest_for(self.asset, file="../outside.bin")
        self.assert_compliance_error(
            "asset.path_escape",
            lambda: verify_manifest(manifest, self.root, trust(manifest)),
            "four-stem-model",
        )

    def test_rejects_unsupported_or_missing_capabilities(self):
        for capabilities in (
            ["vocals", "drums", "bass", "other", "guitar"],
            ["vocals", "drums", "bass"],
        ):
            with self.subTest(capabilities=capabilities):
                manifest = manifest_for(self.asset, capabilities=capabilities)
                self.assert_compliance_error(
                    "manifest.unsupported_capabilities",
                    lambda: verify_manifest(manifest, self.root, trust(manifest)),
                    "four-stem-model",
                )

    def test_rejects_missing_license_evidence(self):
        manifest = manifest_for(self.asset, license={"identifier": "unknown"})
        self.assert_compliance_error(
            "manifest.invalid",
            lambda: verify_manifest(manifest, self.root, trust(manifest)),
            "four-stem-model",
        )

    def test_rejects_unauthorized_asset(self):
        manifest = manifest_for(self.asset, private_non_commercial_authorized=False)
        self.assert_compliance_error(
            "manifest.unauthorized",
            lambda: verify_manifest(manifest, self.root, trust(manifest)),
            "four-stem-model",
        )

    def test_readiness_requires_model_and_runtime(self):
        model = manifest_for(self.asset)
        self.assert_compliance_error(
            "readiness.missing_kind",
            lambda: check_offline_readiness([model], self.root, trust(model)),
        )

    def test_readiness_rejects_network_or_native_claims(self):
        for kwargs, code in (
            ({"network_required": True}, "readiness.network_required"),
            ({"claims": ["native-macos-audio"]}, "readiness.unsupported_claim"),
            ({"claims": ["macos-build"]}, "readiness.unsupported_claim"),
            ({"claims": ["commercial-release"]}, "readiness.unsupported_claim"),
            ({"claims": ["unknown-claim"]}, "readiness.unsupported_claim"),
        ):
            with self.subTest(kwargs=kwargs):
                self.assert_compliance_error(
                    code,
                    lambda: check_offline_readiness([], self.root, {}, **kwargs),
                )

    def test_ready_offline_reports_portable_scope(self):
        runtime_path = self.root / "runtime.bin"
        runtime_path.write_bytes(b"runtime")
        model = manifest_for(self.asset)
        runtime = manifest_for(
            runtime_path,
            asset_id="python-runtime",
            kind="runtime",
        )
        trusted = trust(model) | trust(runtime)

        readiness = check_offline_readiness(
            [model, runtime], self.root, trusted, claims=["portable-contract-only"]
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(ALLOWED_CAPABILITIES, readiness.capabilities)
        self.assertEqual("portable-contract-only", readiness.claim_scope)
        self.assertFalse(readiness.network_required)


def admitted_metal(specialist_id="metal-lead-rhythm-v1"):
    return replace(METAL_PROFILE, specialist_id=specialist_id, enabled=True)


CALIBRATED = SimpleNamespace(calibrated=True)
UNCALIBRATED = SimpleNamespace(calibrated=False)


class ProfileReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def asset(self, asset_id, kind, capabilities):
        path = self.root / f"{asset_id}.bin"
        path.write_bytes(asset_id.encode("ascii"))
        manifest = manifest_for(path, asset_id=asset_id, kind=kind, capabilities=sorted(capabilities))
        return manifest

    def bundle(self, profile, kinds):
        capabilities = profile_capabilities(profile)
        manifests = [self.asset(f"{kind}-asset", kind, capabilities) for kind in kinds]
        trusted = {}
        for manifest in manifests:
            trusted.update(trust(manifest))
        return manifests, trusted

    def test_capabilities_follow_the_profile_layout(self):
        self.assertEqual(ALLOWED_CAPABILITIES, profile_capabilities(LEGACY_PROFILE))
        self.assertEqual(
            frozenset({"vocals", "drums", "bass", "lead_guitar", "rhythm_guitar", "other"}),
            profile_capabilities(METAL_PROFILE),
        )

    def test_metal_reports_the_zero_specialist_state_actionably(self):
        readiness = check_profile_readiness(METAL_PROFILE, [], self.root, {})

        self.assertFalse(readiness.ready)
        self.assertEqual(METAL_PROFILE.profile_id, readiness.profile_id)
        self.assertIn("admit_metal_guitar_model.py", readiness.remediation)
        self.assertEqual((), readiness.assets)

    def test_legacy_is_ready_with_its_registered_assets(self):
        manifests, trusted = self.bundle(LEGACY_PROFILE, ("model", "runtime"))

        readiness = check_profile_readiness(LEGACY_PROFILE, manifests, self.root, trusted)

        self.assertTrue(readiness.ready)
        self.assertEqual(ALLOWED_CAPABILITIES, readiness.capabilities)
        self.assertEqual("", readiness.remediation)

    def test_uncalibrated_role_evidence_keeps_an_admitted_profile_unavailable(self):
        profile = admitted_metal()
        manifests, trusted = self.bundle(profile, ("model", "runtime", "specialist"))

        readiness = check_profile_readiness(
            profile, manifests, self.root, trusted, thresholds=UNCALIBRATED
        )

        self.assertFalse(readiness.ready)
        self.assertIn("calibrated role evidence", readiness.remediation)

    def test_an_admitted_and_calibrated_profile_is_ready(self):
        profile = admitted_metal()
        manifests, trusted = self.bundle(profile, ("model", "runtime", "specialist"))

        readiness = check_profile_readiness(
            profile, manifests, self.root, trusted, thresholds=CALIBRATED
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(profile_capabilities(profile), readiness.capabilities)
        self.assertEqual(3, len(readiness.assets))

    def test_a_missing_specialist_asset_fails_closed(self):
        profile = admitted_metal()
        manifests, trusted = self.bundle(profile, ("model", "runtime"))

        with self.assertRaises(ComplianceError) as caught:
            check_profile_readiness(profile, manifests, self.root, trusted, thresholds=CALIBRATED)

        self.assertEqual("readiness.missing_kind", caught.exception.code)
        self.assertIn("specialist", caught.exception.cause)

    def test_an_asset_declaring_the_wrong_layout_is_rejected(self):
        profile = admitted_metal()
        manifests, trusted = self.bundle(LEGACY_PROFILE, ("model", "runtime", "specialist"))

        with self.assertRaises(ComplianceError) as caught:
            check_profile_readiness(profile, manifests, self.root, trusted, thresholds=CALIBRATED)

        self.assertEqual("manifest.unsupported_capabilities", caught.exception.code)

    def test_tampering_raises_instead_of_reporting_a_routine_unavailability(self):
        profile = admitted_metal()
        manifests, trusted = self.bundle(profile, ("model", "runtime", "specialist"))
        (self.root / "specialist-asset.bin").write_bytes(b"tampered")

        with self.assertRaises(ComplianceError) as caught:
            check_profile_readiness(profile, manifests, self.root, trusted, thresholds=CALIBRATED)

        self.assertEqual("asset.hash_mismatch", caught.exception.code)

    def test_a_network_requirement_is_refused(self):
        manifests, trusted = self.bundle(LEGACY_PROFILE, ("model", "runtime"))

        with self.assertRaises(ComplianceError) as caught:
            check_profile_readiness(
                LEGACY_PROFILE, manifests, self.root, trusted, network_required=True
            )

        self.assertEqual("readiness.network_required", caught.exception.code)

    def test_an_unsupported_claim_is_refused(self):
        manifests, trusted = self.bundle(LEGACY_PROFILE, ("model", "runtime"))

        with self.assertRaises(ComplianceError) as caught:
            check_profile_readiness(
                LEGACY_PROFILE, manifests, self.root, trusted, claims=("macos-verified",)
            )

        self.assertEqual("readiness.unsupported_claim", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
