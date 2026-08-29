import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ALLOWED_CAPABILITIES = frozenset({"vocals", "drums", "bass", "other"})
ALLOWED_CLAIMS = frozenset({"portable-contract-only"})
REQUIRED_KINDS = frozenset({"model", "runtime"})
SPECIALIST_KIND = "specialist"


@dataclass(eq=False)
class ComplianceError(ValueError):
    code: str
    asset: str | None
    cause: str
    recovery: str

    def __str__(self):
        subject = f" [{self.asset}]" if self.asset else ""
        return f"{self.code}{subject}: {self.cause} Recovery: {self.recovery}"


@dataclass(frozen=True)
class VerifiedAsset:
    asset_id: str
    kind: str
    path: Path
    sha256: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class Readiness:
    ready: bool
    assets: tuple[VerifiedAsset, ...]
    capabilities: frozenset[str] = ALLOWED_CAPABILITIES
    claim_scope: str = "portable-contract-only"
    network_required: bool = False


def _fail(code, asset, cause, recovery="Restore the approved bundled assets and retry."):
    raise ComplianceError(code, asset, cause, recovery)


def canonical_manifest_bytes(manifest):
    try:
        return json.dumps(
            manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        _fail("manifest.invalid", None, f"The manifest is not canonical JSON: {error}.")


def _sha256_file(path, asset):
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise ComplianceError(
            "asset.read_failed",
            asset,
            f"Required bundled bytes could not be read at {path.name!r}: {error}.",
            "Check asset permissions or restore the approved bundle and retry.",
        ) from error


def _validate_fields(manifest):
    required = {
        "schema_version", "asset_id", "kind", "file", "version", "origin", "sha256",
        "author", "license", "capabilities", "private_non_commercial_authorized",
    }
    if not isinstance(manifest, dict):
        _fail("manifest.invalid", None, "The manifest must be an object.")
    asset = manifest.get("asset_id")
    missing = sorted(required - manifest.keys())
    if missing:
        _fail("manifest.invalid", asset, f"Required fields are missing: {', '.join(missing)}.")
    text_fields = ("asset_id", "kind", "file", "version", "origin", "sha256", "author")
    if any(not isinstance(manifest[name], str) or not manifest[name] for name in text_fields):
        _fail("manifest.invalid", asset, "Identity, provenance, and hash fields must be non-empty strings.")
    license_data = manifest["license"]
    if not isinstance(license_data, dict) or any(
        not isinstance(license_data.get(name), str) or not license_data[name]
        for name in ("identifier", "evidence", "text")
    ):
        _fail("manifest.invalid", asset, "License identifier, evidence, and text are required.")
    if manifest["schema_version"] != 1:
        _fail("manifest.invalid", asset, "Only manifest schema version 1 is supported.")
    return asset


def verify_manifest(manifest, asset_root, trusted_manifest_hashes, *, capabilities=ALLOWED_CAPABILITIES):
    asset = _validate_fields(manifest)
    expected_manifest_hash = trusted_manifest_hashes.get(asset)
    if expected_manifest_hash is None:
        _fail("manifest.unregistered", asset, "The asset has no trusted manifest registration.")
    actual_manifest_hash = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    if actual_manifest_hash != expected_manifest_hash:
        _fail("manifest.forged", asset, "The manifest differs from its trusted registration.")
    expected_capabilities = frozenset(capabilities)
    declared = manifest["capabilities"]
    if (
        not isinstance(declared, list)
        or set(declared) != expected_capabilities
        or len(declared) != len(expected_capabilities)
    ):
        _fail(
            "manifest.unsupported_capabilities",
            asset,
            f"Capabilities must be exactly {', '.join(sorted(expected_capabilities))}.",
            "Use an approved asset for this profile; unsupported outputs remain unavailable.",
        )
    if manifest["private_non_commercial_authorized"] is not True:
        _fail("manifest.unauthorized", asset, "Private non-commercial authorization is absent.")
    root = Path(asset_root).resolve()
    path = (root / manifest["file"]).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        _fail("asset.path_escape", asset, "The asset path escapes the bundled asset root.")
    if not path.is_file():
        _fail("asset.missing", asset, f"Required bundled bytes are missing at {manifest['file']!r}.")
    digest = _sha256_file(path, asset)
    if digest != manifest["sha256"]:
        _fail("asset.hash_mismatch", asset, "Bundled bytes do not match the registered SHA-256.")
    return VerifiedAsset(asset, manifest["kind"], path, digest, expected_capabilities)


def check_offline_readiness(
    manifests, asset_root, trusted_manifest_hashes, *, network_required=False, claims=()
):
    if network_required:
        _fail(
            "readiness.network_required", None, "Readiness cannot depend on a network request.",
            "Bundle every required byte and run without downloads.",
        )
    unsupported = sorted(set(claims) - ALLOWED_CLAIMS)
    if unsupported:
        _fail(
            "readiness.unsupported_claim", None, f"Portable checks cannot prove: {', '.join(unsupported)}.",
            "Keep the claim disabled until its native or commercial gate is verified.",
        )
    verified = tuple(verify_manifest(item, asset_root, trusted_manifest_hashes) for item in manifests)
    missing_kinds = sorted(REQUIRED_KINDS - {item.kind for item in verified})
    if missing_kinds:
        _fail(
            "readiness.missing_kind", None, f"Required bundled asset kinds are missing: {', '.join(missing_kinds)}.",
            "Bundle and register both the approved runtime and model.",
        )
    return Readiness(True, verified)


@dataclass(frozen=True)
class ProfileReadiness:
    """Whether one stem profile may run, and why not when it may not."""

    profile_id: str
    ready: bool
    assets: tuple
    capabilities: frozenset
    remediation: str = ""
    claim_scope: str = "portable-contract-only"
    network_required: bool = False


def profile_capabilities(profile):
    """Return the capability set one profile's registered assets must cover."""
    return frozenset(profile.lane_ids)


def _unavailable(profile, remediation):
    return ProfileReadiness(profile.profile_id, False, (), profile_capabilities(profile), remediation)


def check_profile_readiness(
    profile,
    manifests,
    asset_root,
    trusted_manifest_hashes,
    *,
    network_required=False,
    claims=(),
    thresholds=None,
):
    """Report whether one profile may run, failing closed and never guessing.

    Expected unavailability is reported with actionable remediation so a view
    can explain it: a disabled profile, a profile whose specialist has never
    been admitted, or one whose role evidence is still uncalibrated.

    Integrity failures still raise, because an unregistered, forged, altered,
    escaped, or missing asset means something is wrong rather than merely
    unavailable, and must never be reported as a routine "not yet".
    """
    needs_specialist = profile.specialist_input is not None
    if needs_specialist and profile.specialist_id is None:
        return _unavailable(
            profile,
            f"No {profile.display_name} specialist has been admitted, so this profile cannot run. "
            "Admit one with Tools/admit_metal_guitar_model.py, then restart Stemslayer.",
        )
    if not profile.enabled:
        return _unavailable(
            profile, f"The {profile.display_name} profile is disabled in this build."
        )
    if needs_specialist and not getattr(thresholds, "calibrated", False):
        return _unavailable(
            profile,
            f"The {profile.display_name} profile has no calibrated role evidence. "
            "Calibrate reconstruction, audibility, and absence limits on a rights-cleared corpus first.",
        )

    if network_required:
        _fail(
            "readiness.network_required", None, "Readiness cannot depend on a network request.",
            "Bundle every required byte and run without downloads.",
        )
    unsupported = sorted(set(claims) - ALLOWED_CLAIMS)
    if unsupported:
        _fail(
            "readiness.unsupported_claim", None, f"Portable checks cannot prove: {', '.join(unsupported)}.",
            "Keep the claim disabled until its native or commercial gate is verified.",
        )
    capabilities = profile_capabilities(profile)
    verified = tuple(
        verify_manifest(item, asset_root, trusted_manifest_hashes, capabilities=capabilities)
        for item in manifests
    )
    required = set(REQUIRED_KINDS) | ({SPECIALIST_KIND} if needs_specialist else set())
    missing_kinds = sorted(required - {item.kind for item in verified})
    if missing_kinds:
        _fail(
            "readiness.missing_kind", None, f"Required bundled asset kinds are missing: {', '.join(missing_kinds)}.",
            "Bundle and register every approved asset this profile requires.",
        )
    return ProfileReadiness(profile.profile_id, True, verified, capabilities)
