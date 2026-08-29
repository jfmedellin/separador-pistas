import math
import struct
from dataclasses import dataclass


@dataclass(eq=False)
class ExportError(ValueError):
    code: str
    cause: str
    recovery: str
    peak: float | None = None

    def __str__(self):
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


def encode_float32_wav(audio):
    peak = max(abs(sample) for channel in audio.planar for sample in channel)
    if not math.isfinite(peak) or peak > 1.0:
        raise ExportError(
            "export.clipping",
            f"Export peak {peak:.9g} exceeds the allowed absolute peak of 1.",
            "Reduce mixer gain and export again; no normalization or limiting was applied.",
            peak,
        )

    interleaved = bytearray()
    for frame in range(audio.frame_count):
        for channel in range(audio.channel_count):
            interleaved.extend(struct.pack("<f", audio.planar[channel][frame]))
    block_align = audio.channel_count * 4
    byte_rate = audio.sample_rate * block_align
    fmt = struct.pack("<HHIIHH", 3, audio.channel_count, audio.sample_rate, byte_rate, block_align, 32)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(interleaved))
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(interleaved)) + bytes(interleaved)
