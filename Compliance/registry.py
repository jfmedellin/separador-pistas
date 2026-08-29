import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ALLOWED_CAPABILITIES = frozenset({"vocals", "drums", "bass", "other"})
ALLOWED_CLAIMS = frozenset({"portable-contract-only"})
REQUIRED_KINDS = frozenset({"model", "runtime"})


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


def verify_manifest(manifest, asset_root, trusted_manifest_hashes):
    asset = _validate_fields(manifest)
    expected_manifest_hash = trusted_manifest_hashes.get(asset)
    if expected_manifest_hash is None:
        _fail("manifest.unregistered", asset, "The asset has no trusted manifest registration.")
    actual_manifest_hash = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    if actual_manifest_hash != expected_manifest_hash:
        _fail("manifest.forged", asset, "The manifest differs from its trusted registration.")
    capabilities = manifest["capabilities"]
    if not isinstance(capabilities, list) or set(capabilities) != ALLOWED_CAPABILITIES or len(capabilities) != 4:
        _fail(
            "manifest.unsupported_capabilities",
            asset,
            "Capabilities must be exactly vocals, drums, bass, and other.",
            "Use an approved four-capability asset; unsupported outputs remain unavailable.",
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
    return VerifiedAsset(asset, manifest["kind"], path, digest, ALLOWED_CAPABILITIES)


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
