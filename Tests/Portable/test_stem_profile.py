import unittest
from dataclasses import replace

from SeparationWorker.engine.stem_profile import (
    LEGACY_PROFILE,
    LEGACY_PROFILE_ID,
    MANIFEST_SCHEMA_VERSION,
    METAL_PROFILE,
    METAL_PROFILE_ID,
    PROFILES,
    StemLane,
    StemProfile,
    StemProfileError,
    build_manifest,
    parse_manifest,
    resolve_profile,
    verify_manifest,
)


def admitted_metal(specialist_id="metal-lead-rhythm-v1"):
    """Return the Metal profile as it would look once a specialist is admitted."""
    return replace(METAL_PROFILE, specialist_id=specialist_id, enabled=True)


class LegacyProfileTests(unittest.TestCase):
    def test_legacy_layout_and_order_are_unchanged(self):
        self.assertEqual(("vocals", "drums", "bass", "other"), LEGACY_PROFILE.lane_ids)
        self.assertEqual(
            ("vocals.wav", "drums.wav", "bass.wav", "other.wav"), LEGACY_PROFILE.file_names
        )
        self.assertEqual("htdemucs", LEGACY_PROFILE.primary_model)

    def test_legacy_is_enabled_and_accepts_manifestless_results(self):
        self.assertTrue(LEGACY_PROFILE.enabled)
        self.assertTrue(LEGACY_PROFILE.accepts_manifestless_results)
        self.assertIsNone(LEGACY_PROFILE.specialist_input)

    def test_legacy_declares_other_as_its_residual(self):
        self.assertEqual("other", LEGACY_PROFILE.residual_lane.lane_id)
        self.assertEqual((), LEGACY_PROFILE.residual_sources)

    def test_no_legacy_lane_may_publish_as_silence(self):
        self.assertTrue(all(not lane.absentable for lane in LEGACY_PROFILE.lanes))


class MetalProfileTests(unittest.TestCase):
    def test_metal_publishes_six_ordered_lanes(self):
        self.assertEqual(
            ("vocals", "drums", "bass", "lead_guitar", "rhythm_guitar", "other"),
            METAL_PROFILE.lane_ids,
        )

    def test_combined_guitar_is_a_raw_output_and_never_a_published_lane(self):
        self.assertIn("guitar", METAL_PROFILE.raw_outputs)
        self.assertNotIn("guitar", METAL_PROFILE.lane_ids)
        self.assertEqual("guitar", METAL_PROFILE.specialist_input)

    def test_guitar_children_are_the_only_role_lanes(self):
        self.assertEqual(
            ("lead_guitar", "rhythm_guitar"),
            tuple(lane.lane_id for lane in METAL_PROFILE.role_lanes("guitar")),
        )

    def test_role_lanes_may_publish_as_silence_and_other_lanes_may_not(self):
        self.assertTrue(all(lane.absentable for lane in METAL_PROFILE.role_lanes("guitar")))
        self.assertFalse(METAL_PROFILE.lane("vocals").absentable)
        self.assertFalse(METAL_PROFILE.lane("other").absentable)

    def test_metal_folds_piano_and_primary_other_into_the_residual(self):
        self.assertEqual(("piano", "other"), METAL_PROFILE.residual_sources)
        self.assertEqual("other", METAL_PROFILE.residual_lane.lane_id)

    def test_metal_ships_disabled_with_no_registered_specialist(self):
        self.assertFalse(METAL_PROFILE.enabled)
        self.assertIsNone(METAL_PROFILE.specialist_id)
        self.assertFalse(METAL_PROFILE.accepts_manifestless_results)

    def test_metal_cannot_be_enabled_without_admitting_a_specialist(self):
        with self.assertRaises(StemProfileError) as caught:
            replace(METAL_PROFILE, enabled=True)

        self.assertEqual("profile.enabled_without_specialist", caught.exception.code)

    def test_metal_enables_once_a_specialist_is_registered(self):
        self.assertTrue(admitted_metal().enabled)


class ProfileValidationTests(unittest.TestCase):
    def build(self, **overrides):
        defaults = {
            "profile_id": "test",
            "display_name": "Test",
            "lanes": (StemLane("vocals", "Vocals"), StemLane("other", "Other", residual=True)),
            "primary_model": "htdemucs",
            "raw_outputs": ("vocals", "other"),
        }
        return StemProfile(**{**defaults, **overrides})

    def test_valid_profile_builds(self):
        self.assertEqual(("vocals", "other"), self.build().lane_ids)

    def test_empty_lane_set_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            self.build(lanes=())

        self.assertEqual("profile.empty", caught.exception.code)

    def test_duplicate_lane_identifier_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            self.build(lanes=(StemLane("vocals", "Vocals"), StemLane("vocals", "Vocals Again")))

        self.assertEqual("profile.duplicate_lane", caught.exception.code)

    def test_more_than_one_residual_lane_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            self.build(
                lanes=(
                    StemLane("other", "Other", residual=True),
                    StemLane("rest", "Rest", residual=True),
                )
            )

        self.assertEqual("profile.multiple_residuals", caught.exception.code)

    def test_residual_source_the_model_never_emits_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            self.build(residual_sources=("piano",))

        self.assertEqual("profile.unknown_residual_source", caught.exception.code)

    def test_folding_without_a_residual_lane_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            self.build(lanes=(StemLane("vocals", "Vocals"),), residual_sources=("vocals",))

        self.assertEqual("profile.missing_residual_lane", caught.exception.code)

    def test_specialist_input_the_model_never_emits_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            self.build(specialist_input="guitar", enabled=False)

        self.assertEqual("profile.unknown_specialist_input", caught.exception.code)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_for_the_same_profile(self):
        self.assertEqual(LEGACY_PROFILE.pipeline_fingerprint, LEGACY_PROFILE.pipeline_fingerprint)
        self.assertEqual(64, len(LEGACY_PROFILE.pipeline_fingerprint))

    def test_profiles_never_share_a_fingerprint(self):
        self.assertNotEqual(LEGACY_PROFILE.pipeline_fingerprint, METAL_PROFILE.pipeline_fingerprint)

    def test_admitting_a_specialist_changes_the_fingerprint(self):
        self.assertNotEqual(METAL_PROFILE.pipeline_fingerprint, admitted_metal().pipeline_fingerprint)

    def test_a_different_specialist_changes_the_fingerprint(self):
        self.assertNotEqual(
            admitted_metal("v1").pipeline_fingerprint, admitted_metal("v2").pipeline_fingerprint
        )


class ProfileRegistryTests(unittest.TestCase):
    def test_registered_profiles_resolve_by_identifier(self):
        self.assertIs(LEGACY_PROFILE, resolve_profile(LEGACY_PROFILE_ID))
        self.assertIs(METAL_PROFILE, resolve_profile(METAL_PROFILE_ID))
        self.assertEqual({LEGACY_PROFILE_ID, METAL_PROFILE_ID}, set(PROFILES))

    def test_unknown_profile_is_rejected_actionably(self):
        with self.assertRaises(StemProfileError) as caught:
            resolve_profile("nope")

        self.assertEqual("profile.unknown", caught.exception.code)
        self.assertIn(LEGACY_PROFILE_ID, caught.exception.recovery)


class ManifestBuildTests(unittest.TestCase):
    def test_complete_legacy_result_publishes_every_lane(self):
        manifest = build_manifest(LEGACY_PROFILE)

        self.assertEqual(LEGACY_PROFILE.file_names, manifest.file_names)
        self.assertEqual((), manifest.absent_lane_ids)
        self.assertEqual(LEGACY_PROFILE.pipeline_fingerprint, manifest.pipeline_fingerprint)
        self.assertEqual(MANIFEST_SCHEMA_VERSION, manifest.schema_version)

    def test_absent_lead_publishes_as_declared_silence_in_profile_order(self):
        manifest = build_manifest(
            admitted_metal(), absent_lanes={"lead_guitar": "no lead guitar in this track"}
        )

        self.assertEqual(("lead_guitar",), manifest.absent_lane_ids)
        self.assertIn("lead_guitar.wav", manifest.file_names)
        self.assertEqual(admitted_metal().file_names, manifest.file_names)
        lead = next(lane for lane in manifest.lanes if lane.lane_id == "lead_guitar")
        self.assertEqual("no lead guitar in this track", lead.absence_reason)

    def test_absent_lane_without_a_reason_is_rejected(self):
        for reason in ("", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(StemProfileError) as caught:
                    build_manifest(admitted_metal(), absent_lanes={"lead_guitar": reason})

                self.assertEqual("manifest.missing_absence_reason", caught.exception.code)

    def test_a_lane_that_may_not_be_silent_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            build_manifest(admitted_metal(), absent_lanes={"vocals": "quiet song"})

        self.assertEqual("manifest.lane_not_absentable", caught.exception.code)

    def test_negligible_residual_may_be_omitted(self):
        manifest = build_manifest(admitted_metal(), omitted=("other",))

        self.assertNotIn("other.wav", manifest.file_names)
        self.assertEqual(("other",), manifest.omitted)
        self.assertEqual(5, len(manifest.lanes))

    def test_a_role_lane_may_never_be_omitted(self):
        with self.assertRaises(StemProfileError) as caught:
            build_manifest(admitted_metal(), omitted=("lead_guitar",))

        self.assertEqual("manifest.lane_not_omittable", caught.exception.code)

    def test_a_lane_cannot_be_both_absent_and_omitted(self):
        with self.assertRaises(StemProfileError) as caught:
            build_manifest(
                admitted_metal(), absent_lanes={"other": "negligible"}, omitted=("other",)
            )

        self.assertEqual("manifest.conflicting_lane", caught.exception.code)

    def test_unknown_lane_is_rejected(self):
        with self.assertRaises(StemProfileError) as caught:
            build_manifest(LEGACY_PROFILE, absent_lanes={"lead_guitar": "absent"})

        self.assertEqual("profile.unknown_lane", caught.exception.code)


class ManifestRoundTripTests(unittest.TestCase):
    def test_manifest_survives_serialization_and_verification(self):
        profile = admitted_metal()
        original = build_manifest(
            profile,
            absent_lanes={"lead_guitar": "no lead guitar in this track"},
            validation={"reconstruction_db": 41.2},
        )

        restored = verify_manifest(parse_manifest(original.to_json_bytes()), profile)

        self.assertEqual(original.file_names, restored.file_names)
        self.assertEqual(("lead_guitar",), restored.absent_lane_ids)
        self.assertEqual(41.2, restored.validation["reconstruction_db"])

    def test_serialization_is_canonical_and_deterministic(self):
        first = build_manifest(LEGACY_PROFILE).to_json_bytes()
        second = build_manifest(LEGACY_PROFILE).to_json_bytes()

        self.assertEqual(first, second)
        self.assertNotIn(b" ", first)

    def test_manifest_from_another_profile_is_rejected(self):
        legacy = build_manifest(LEGACY_PROFILE)

        with self.assertRaises(StemProfileError) as caught:
            verify_manifest(parse_manifest(legacy.to_json_bytes()), admitted_metal())

        self.assertEqual("manifest.profile_mismatch", caught.exception.code)

    def test_manifest_from_another_pipeline_identity_is_rejected(self):
        produced = build_manifest(admitted_metal("v1"))

        with self.assertRaises(StemProfileError) as caught:
            verify_manifest(parse_manifest(produced.to_json_bytes()), admitted_metal("v2"))

        self.assertEqual("manifest.fingerprint_mismatch", caught.exception.code)

    def test_reordered_lanes_are_rejected(self):
        profile = admitted_metal()
        manifest = build_manifest(profile)
        payload = manifest.to_json_bytes().replace(
            b'{"absence_reason":null,"absent":false,"lane_id":"vocals"},'
            b'{"absence_reason":null,"absent":false,"lane_id":"drums"}',
            b'{"absence_reason":null,"absent":false,"lane_id":"drums"},'
            b'{"absence_reason":null,"absent":false,"lane_id":"vocals"}',
        )
        self.assertNotEqual(manifest.to_json_bytes(), payload)

        with self.assertRaises(StemProfileError) as caught:
            verify_manifest(parse_manifest(payload), profile)

        self.assertEqual("manifest.layout_mismatch", caught.exception.code)

    def test_unsupported_schema_version_is_rejected(self):
        payload = build_manifest(LEGACY_PROFILE).to_json_bytes().replace(
            b'"schema_version":1', b'"schema_version":2'
        )

        with self.assertRaises(StemProfileError) as caught:
            parse_manifest(payload)

        self.assertEqual("manifest.unsupported_schema", caught.exception.code)

    def test_unreadable_manifest_is_rejected(self):
        for payload in (b"not json", b'"a string"', b'{"schema_version":1,"lanes":[]}'):
            with self.subTest(payload=payload):
                with self.assertRaises(StemProfileError) as caught:
                    parse_manifest(payload)

                self.assertIn(
                    caught.exception.code, {"manifest.unreadable", "manifest.unsupported_schema"}
                )


if __name__ == "__main__":
    unittest.main()
