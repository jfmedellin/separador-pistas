import hashlib
import tempfile
from pathlib import Path

from Compliance.registry import canonical_manifest_bytes, check_offline_readiness
from SeparationWorker.protocol.framing import decode_frame, encode_frame
from SeparationWorker.protocol.schemas import CAPABILITIES, PROTOCOL_MAJOR, validate_message


def _manifest(path, asset_id, kind):
    return {
        "schema_version": 1,
        "asset_id": asset_id,
        "kind": kind,
        "file": path.name,
        "version": "self-test",
        "origin": "generated-local-self-test",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "author": "Limbus Split Pro self-test",
        "license": {
            "identifier": "LicenseRef-Self-Test",
            "evidence": "generated-in-memory",
            "text": "Ephemeral self-test bytes only.",
        },
        "capabilities": sorted(CAPABILITIES),
        "private_non_commercial_authorized": True,
    }


def main():
    hello = {
        "type": "hello",
        "protocol": {"major": PROTOCOL_MAJOR},
        "worker": "portable-self-test",
        "runtime": "stdlib-only",
        "capabilities": sorted(CAPABILITIES),
    }
    validate_message(decode_frame(encode_frame(hello)))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        model_path, runtime_path = root / "model.bin", root / "runtime.bin"
        model_path.write_bytes(b"self-test-model")
        runtime_path.write_bytes(b"self-test-runtime")
        manifests = [
            _manifest(model_path, "self-test-model", "model"),
            _manifest(runtime_path, "self-test-runtime", "runtime"),
        ]
        trusted = {
            item["asset_id"]: hashlib.sha256(canonical_manifest_bytes(item)).hexdigest()
            for item in manifests
        }
        readiness = check_offline_readiness(manifests, root, trusted)
        assert readiness.ready and not readiness.network_required
    print("PASS: protocol v1 framing and portable offline readiness")


if __name__ == "__main__":
    main()
