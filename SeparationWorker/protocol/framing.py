import json
import struct

from .errors import protocol_error

MAX_FRAME_BYTES = 1024 * 1024


def _canonical_bytes(message):
    try:
        return json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise protocol_error("frame.not_serializable", str(error)) from error


def encode_frame(message):
    if not isinstance(message, dict):
        raise protocol_error("frame.not_object", "A control message must be a JSON object.")
    payload = _canonical_bytes(message)
    if len(payload) > MAX_FRAME_BYTES:
        raise protocol_error(
            "frame.too_large",
            f"The payload is {len(payload)} bytes; the limit is {MAX_FRAME_BYTES}.",
            "Reduce the control message size; transfer PCM through files.",
        )
    return struct.pack(">I", len(payload)) + payload


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise protocol_error("frame.duplicate_key", f"JSON key {key!r} occurs more than once.")
        result[key] = value
    return result


def decode_frame(frame):
    if not isinstance(frame, bytes):
        raise protocol_error("frame.invalid_type", "A frame must be bytes.")
    if len(frame) < 4:
        raise protocol_error("frame.truncated_prefix", "The four-byte length prefix is incomplete.")
    declared = struct.unpack(">I", frame[:4])[0]
    if declared > MAX_FRAME_BYTES:
        raise protocol_error(
            "frame.too_large",
            f"The declared payload is {declared} bytes; the limit is {MAX_FRAME_BYTES}.",
            "Reject the sender and use bounded protocol v1 controls.",
        )
    if len(frame) - 4 != declared:
        raise protocol_error(
            "frame.length_mismatch",
            f"Declared {declared} payload bytes but received {len(frame) - 4}.",
            "Send exactly one complete length-prefixed frame.",
        )
    payload = frame[4:]
    try:
        message = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except UnicodeDecodeError as error:
        raise protocol_error("frame.invalid_utf8", "The payload is not valid UTF-8.") from error
    except json.JSONDecodeError as error:
        raise protocol_error("frame.invalid_json", f"The payload is not valid JSON: {error.msg}.") from error
    if not isinstance(message, dict):
        raise protocol_error("frame.not_object", "A control message must be a JSON object.")
    if payload != _canonical_bytes(message):
        raise protocol_error(
            "frame.noncanonical",
            "The JSON payload is not in canonical sorted, compact form.",
            "Encode the message with the protocol canonical JSON encoder.",
        )
    return message
