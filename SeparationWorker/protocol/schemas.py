from .errors import protocol_error

PROTOCOL_MAJOR = 1
CAPABILITIES = frozenset({"vocals", "drums", "bass", "other"})


def _required(mapping, names, context):
    if not isinstance(mapping, dict):
        raise protocol_error("schema.invalid_type", f"{context} must be an object.")
    missing = [name for name in names if name not in mapping]
    if missing:
        raise protocol_error(
            "schema.missing_field",
            f"{context} is missing required field(s): {', '.join(missing)}.",
            "Provide every required protocol v1 field and retry.",
        )


def _strings(mapping, names, context):
    for name in names:
        if not isinstance(mapping[name], str) or not mapping[name]:
            raise protocol_error("schema.invalid_type", f"{context}.{name} must be a non-empty string.")


def _validate_error(error):
    fields = ("code", "stage", "cause", "recovery", "retryable")
    _required(error, fields, "error")
    _strings(error, fields[:4], "error")
    if not isinstance(error["retryable"], bool):
        raise protocol_error("schema.invalid_type", "error.retryable must be a boolean.")
    if "asset" in error and (not isinstance(error["asset"], str) or not error["asset"]):
        raise protocol_error("schema.invalid_type", "error.asset must be a non-empty string when present.")


def _validate_hello(message):
    _required(message, ("protocol", "worker", "runtime", "capabilities"), "hello")
    protocol = message["protocol"]
    _required(protocol, ("major",), "hello.protocol")
    if protocol["major"] != PROTOCOL_MAJOR:
        raise protocol_error(
            "protocol.unsupported_major",
            f"Protocol major {protocol['major']!r} is unsupported; expected {PROTOCOL_MAJOR}.",
            "Use a worker and client that both implement protocol v1.",
        )
    _strings(message, ("worker", "runtime"), "hello")
    capabilities = message["capabilities"]
    if not isinstance(capabilities, list) or set(capabilities) != CAPABILITIES or len(capabilities) != 4:
        raise protocol_error(
            "schema.unsupported_capabilities",
            "hello must advertise exactly vocals, drums, bass, and other.",
            "Use the approved four-capability worker.",
        )
    if "identities" in message:
        identities = message["identities"]
        _required(identities, ("worker", "runtime", "model"), "hello.identities")
        for name in ("worker", "runtime", "model"):
            identity = identities[name]
            _required(identity, ("id", "sha256"), f"hello.identities.{name}")
            _strings(identity, ("id", "sha256"), f"hello.identities.{name}")
            digest = identity["sha256"]
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise protocol_error(
                    "schema.invalid_hash",
                    f"hello.identities.{name}.sha256 must be a lowercase SHA-256 digest.",
                )


def validate_message(message):
    _required(message, ("type",), "message")
    _strings(message, ("type",), "message")
    kind = message["type"]
    if kind == "hello":
        _validate_hello(message)
    elif kind == "start":
        _required(message, ("job", "input", "workspace", "model"), "start")
        _strings(message, ("job", "input", "workspace", "model"), "start")
    elif kind in {"heartbeat", "progress"}:
        _required(message, ("job", "stage"), kind)
        _strings(message, ("job", "stage"), kind)
        measured = "completed" in message or "total" in message
        if kind == "heartbeat" and measured:
            raise protocol_error("schema.invalid_progress", "Heartbeat frames cannot contain progress counts.")
        if measured and not all(name in message for name in ("completed", "total")):
            raise protocol_error("schema.invalid_progress", "Measured progress requires completed and total.")
        if measured:
            completed, total = message["completed"], message["total"]
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in (completed, total)):
                raise protocol_error("schema.invalid_progress", "Progress counts must be integers.")
            if completed < 0 or total <= 0 or completed > total:
                raise protocol_error("schema.invalid_progress", "Progress must satisfy 0 <= completed <= total.")
    elif kind in {"cancel", "cancelled"}:
        _required(message, ("job",), kind)
        _strings(message, ("job",), kind)
    elif kind == "completed":
        _required(message, ("job", "manifest"), "completed")
        _strings(message, ("job",), "completed")
        if not isinstance(message["manifest"], dict):
            raise protocol_error("schema.invalid_type", "completed.manifest must be an object.")
    elif kind == "failed":
        _required(message, ("job", "error"), "failed")
        _strings(message, ("job",), "failed")
        _validate_error(message["error"])
    else:
        raise protocol_error(
            "schema.unknown_message",
            f"Message type {kind!r} is not part of protocol v1.",
            "Use hello, start, heartbeat, progress, cancel, completed, failed, or cancelled.",
        )
    return message
