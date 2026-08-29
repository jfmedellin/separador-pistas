import json
import struct
import unittest

from SeparationWorker.protocol.errors import ProtocolError
from SeparationWorker.protocol.framing import MAX_FRAME_BYTES, decode_frame, encode_frame
from SeparationWorker.protocol.schemas import validate_message


class FramingTests(unittest.TestCase):
    def assert_protocol_error(self, code, operation):
        with self.assertRaises(ProtocolError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertTrue(caught.exception.cause)
        self.assertTrue(caught.exception.recovery)

    def test_round_trips_canonical_json(self):
        message = {"worker": "portable", "type": "hello", "protocol": {"major": 1}}
        frame = encode_frame(message)

        self.assertEqual(len(frame) - 4, struct.unpack(">I", frame[:4])[0])
        self.assertEqual(message, decode_frame(frame))
        self.assertEqual(
            b'{"protocol":{"major":1},"type":"hello","worker":"portable"}',
            frame[4:],
        )

    def test_rejects_truncated_prefix(self):
        self.assert_protocol_error("frame.truncated_prefix", lambda: decode_frame(b"\x00\x00"))

    def test_rejects_truncated_payload(self):
        frame = struct.pack(">I", 20) + b"{}"
        self.assert_protocol_error("frame.length_mismatch", lambda: decode_frame(frame))

    def test_rejects_trailing_bytes(self):
        frame = struct.pack(">I", 2) + b"{}extra"
        self.assert_protocol_error("frame.length_mismatch", lambda: decode_frame(frame))

    def test_rejects_oversized_declared_payload_before_parsing(self):
        frame = struct.pack(">I", MAX_FRAME_BYTES + 1)
        self.assert_protocol_error("frame.too_large", lambda: decode_frame(frame))

    def test_rejects_noncanonical_json(self):
        payload = b'{"type": "cancel", "job":"j-1"}'
        frame = struct.pack(">I", len(payload)) + payload
        self.assert_protocol_error("frame.noncanonical", lambda: decode_frame(frame))

    def test_rejects_duplicate_keys(self):
        payload = b'{"type":"cancel","type":"start","job":"j-1"}'
        frame = struct.pack(">I", len(payload)) + payload
        self.assert_protocol_error("frame.duplicate_key", lambda: decode_frame(frame))

    def test_rejects_non_object_json(self):
        payload = b"[]"
        frame = struct.pack(">I", len(payload)) + payload
        self.assert_protocol_error("frame.not_object", lambda: decode_frame(frame))

    def test_rejects_non_json_payload(self):
        payload = b"not-json"
        frame = struct.pack(">I", len(payload)) + payload
        self.assert_protocol_error("frame.invalid_json", lambda: decode_frame(frame))


class SchemaTests(unittest.TestCase):
    def test_accepts_v1_hello_and_actionable_failure(self):
        hello = validate_message(
            {
                "type": "hello",
                "protocol": {"major": 1},
                "worker": "1.0.0",
                "runtime": "cpython-portable",
                "capabilities": ["vocals", "drums", "bass", "other"],
            }
        )
        failure = validate_message(
            {
                "type": "failed",
                "job": "job-1",
                "error": {
                    "code": "asset.hash_mismatch",
                    "stage": "validating",
                    "asset": "model.bin",
                    "cause": "The bundled model hash did not match its manifest.",
                    "recovery": "Restore the approved application bundle.",
                    "retryable": False,
                },
            }
        )

        self.assertEqual("hello", hello["type"])
        self.assertFalse(failure["error"]["retryable"])

    def test_rejects_wrong_protocol_major_actionably(self):
        message = {
            "type": "hello",
            "protocol": {"major": 2},
            "worker": "1.0.0",
            "runtime": "portable",
            "capabilities": ["vocals", "drums", "bass", "other"],
        }
        self.assert_protocol_error("protocol.unsupported_major", lambda: validate_message(message))

    def test_rejects_missing_actionable_error_fields(self):
        message = {
            "type": "failed",
            "job": "job-1",
            "error": {"code": "failed", "stage": "running", "retryable": False},
        }
        self.assert_protocol_error("schema.missing_field", lambda: validate_message(message))

    def test_rejects_unknown_message_type(self):
        self.assert_protocol_error(
            "schema.unknown_message",
            lambda: validate_message({"type": "download", "url": "https://example.invalid"}),
        )

    def assert_protocol_error(self, code, operation):
        with self.assertRaises(ProtocolError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertTrue(caught.exception.recovery)


if __name__ == "__main__":
    unittest.main()
