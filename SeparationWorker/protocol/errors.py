from dataclasses import dataclass


@dataclass(eq=False)
class ProtocolError(ValueError):
    """A fail-closed protocol error suitable for display or serialization."""

    code: str
    cause: str
    recovery: str

    def __str__(self):
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


def protocol_error(code, cause, recovery="Use a protocol v1 message and retry."):
    return ProtocolError(code, cause, recovery)
