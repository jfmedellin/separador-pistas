import math
import struct
from dataclasses import dataclass


@dataclass(eq=False)
class AudioContractError(ValueError):
    code: str
    cause: str
    recovery: str

    def __str__(self):
        return f"{self.code}: {self.cause} Recovery: {self.recovery}"


def _error(code, cause, recovery):
    return AudioContractError(code, cause, recovery)


def _float32(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _error(
            "pcm.non_finite",
            "Planar PCM contains a non-finite or non-numeric sample.",
            "Decode the source again and provide only finite Float32 samples.",
        )
    try:
        rounded = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise _error(
            "pcm.non_finite",
            "A planar PCM sample is outside the finite Float32 range.",
            "Scale or reject the invalid source before processing.",
        ) from exc
    if not math.isfinite(rounded):
        raise _error(
            "pcm.non_finite",
            "A planar PCM sample becomes non-finite in Float32.",
            "Scale or reject the invalid source before processing.",
        )
    return rounded


@dataclass(frozen=True)
class PlanarPCM:
    sample_rate: int
    planar: tuple
    frame_zero: int = 0

    def __post_init__(self):
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise _error(
                "pcm.sample_rate",
                "PCM sample rate must be a positive integer.",
                "Preserve the decoded source sample rate.",
            )
        if self.frame_zero != 0:
            raise _error(
                "pcm.frame_zero",
                "Canonical PCM must begin at source frame zero.",
                "Align and trim every stem to source frame zero.",
            )
        try:
            channels = tuple(tuple(_float32(sample) for sample in channel) for channel in self.planar)
        except TypeError as exc:
            raise _error(
                "pcm.planar",
                "PCM must be an iterable of planar channels.",
                "Provide one sample sequence per source channel.",
            ) from exc
        if not channels or not channels[0]:
            raise _error(
                "pcm.empty",
                "PCM requires at least one channel and one frame.",
                "Decode a non-empty supported source before processing.",
            )
        frame_count = len(channels[0])
        if any(len(channel) != frame_count for channel in channels):
            raise _error(
                "pcm.channel_misalignment",
                "Planar PCM channels do not have the same frame count.",
                "Align every channel to the decoded source frame count.",
            )
        object.__setattr__(self, "planar", channels)

    @property
    def channel_count(self):
        return len(self.planar)

    @property
    def frame_count(self):
        return len(self.planar[0])

    @property
    def identity(self):
        return (self.frame_zero, self.sample_rate, self.channel_count, self.frame_count)


@dataclass(frozen=True)
class ResidualResult:
    pcm: PlanarPCM | None
    omission_reason: str | None
    peak: float


def require_common_identity(reference, *others):
    for candidate in others:
        if candidate.identity != reference.identity:
            raise _error(
                "pcm.identity_mismatch",
                "PCM frame zero, rate, channel count, or frame count does not match the source.",
                "Resample and deterministically align every stem to the source identity.",
            )


def compute_residual(source, vocals, drums, bass, *, synthetic_negligible_peak=None):
    """Compute Other in binary64 and round each result once to binary32."""
    require_common_identity(source, vocals, drums, bass)
    if synthetic_negligible_peak is not None:
        if not math.isfinite(synthetic_negligible_peak) or synthetic_negligible_peak < 0:
            raise _error(
                "pcm.invalid_negligible_peak",
                "The synthetic fixture residual boundary is invalid.",
                "Use a finite non-negative fixture-only boundary.",
            )

    channels = []
    peak = 0.0
    for channel_index in range(source.channel_count):
        output = []
        samples = zip(
            source.planar[channel_index],
            vocals.planar[channel_index],
            drums.planar[channel_index],
            bass.planar[channel_index],
        )
        for source_sample, vocal, drum, bass_sample in samples:
            residual64 = float(source_sample) - (float(vocal) + float(drum) + float(bass_sample))
            residual32 = _float32(residual64)
            output.append(residual32)
            peak = max(peak, abs(residual32))
        channels.append(tuple(output))

    if synthetic_negligible_peak is not None and peak <= synthetic_negligible_peak:
        return ResidualResult(
            None,
            "Omitted by a synthetic fixture-only boundary; CALIBRATION REQUIRED before quality use.",
            peak,
        )
    return ResidualResult(PlanarPCM(source.sample_rate, tuple(channels)), None, peak)
