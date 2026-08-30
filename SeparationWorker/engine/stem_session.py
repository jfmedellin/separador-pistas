"""Validated, streamable metadata for one published Windows mixer result.

A result carries its own layout. When a manifest is present the session
publishes exactly the lanes it declares, in its order, and remembers which
of them are declared absent. A manifest-less folder is the legacy four-stem
layout, which is still loaded exactly as it always was.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .pcm import AudioContractError
from .stem_profile import (
    LEGACY_PROFILE,
    MANIFEST_NAME,
    StemProfileError,
    parse_manifest,
    resolve_profile,
    verify_manifest,
)


STEM_ORDER = ("vocals", "drums", "bass", "other")
STEM_NAMES = tuple(f"{name}.wav" for name in STEM_ORDER)
PEAK_BIN_COUNT = 2_000
_READ_BLOCK_FRAMES = 65_536


class StemSessionError(AudioContractError):
    """Actionable error raised when a mixer stem set cannot be loaded."""


def _error(code: str, cause: str, recovery: str) -> StemSessionError:
    return StemSessionError(code, cause, recovery)


def _peak_envelope(reader: sf.SoundFile, frame_count: int, bin_count: int) -> tuple[float, ...]:
    peaks = np.zeros(bin_count, dtype=np.float32)
    position = 0
    while position < frame_count:
        block = reader.read(
            min(_READ_BLOCK_FRAMES, frame_count - position),
            dtype="float32",
            always_2d=True,
        )
        if len(block) == 0:
            break
        if not np.isfinite(block).all():
            raise _error(
                "stem.non_finite",
                f"{reader.name} contains a non-finite audio sample.",
                "Regenerate the stem as a finite PCM WAV and load the complete result folder.",
            )
        frame_peaks = np.max(np.abs(block), axis=1)
        indexes = ((np.arange(len(block), dtype=np.int64) + position) * bin_count) // frame_count
        np.maximum.at(peaks, indexes, frame_peaks)
        position += len(block)

    if position != frame_count:
        raise _error(
            "stem.unreadable",
            f"{reader.name} ended at frame {position}, expected {frame_count}.",
            "Replace the truncated stem with a complete readable WAV and load the folder again.",
        )
    return tuple(float(value) for value in peaks)


@dataclass(frozen=True)
class StemSession:
    """Immutable metadata and waveform envelopes for one aligned stem folder."""

    folder: Path
    paths: tuple[Path, ...]
    sample_rate: int
    channels: int
    frame_count: int
    peaks: tuple[tuple[float, ...], ...]
    names: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    profile_id: str = LEGACY_PROFILE.profile_id

    def __post_init__(self):
        object.__setattr__(self, "folder", Path(self.folder).resolve())
        object.__setattr__(self, "paths", tuple(Path(path) for path in self.paths))
        object.__setattr__(self, "peaks", tuple(tuple(float(value) for value in envelope) for envelope in self.peaks))
        if not self.names:
            object.__setattr__(self, "names", tuple(path.name for path in self.paths))
        object.__setattr__(self, "absent", tuple(self.absent))

    @staticmethod
    def _layout(folder: Path, profile) -> tuple[tuple[str, ...], tuple[str, ...], str]:
        """Return the published lane names, absent lanes, and profile identity."""
        manifest_file = folder / MANIFEST_NAME
        try:
            has_manifest = manifest_file.is_file()
            payload = manifest_file.read_bytes() if has_manifest else b""
        except OSError as exc:
            raise _error(
                "stem.manifest_unreadable",
                f"The result manifest could not be read: {exc}.",
                "Discard the result folder and separate the song again.",
            ) from exc
        if not has_manifest:
            if profile is not None and not profile.accepts_manifestless_results:
                raise _error(
                    "stem.manifest_missing",
                    f"The {profile.display_name} profile requires a result manifest.",
                    "Separate the song again; only legacy results may load without a manifest.",
                )
            return STEM_NAMES, (), LEGACY_PROFILE.profile_id
        try:
            manifest = parse_manifest(payload)
            verify_manifest(manifest, profile or resolve_profile(manifest.profile_id))
        except StemProfileError as exc:
            raise _error(
                "stem.manifest_invalid",
                f"The result manifest is not usable ({exc.code}): {exc.cause}",
                exc.recovery,
            ) from exc
        return manifest.file_names, manifest.absent_lane_ids, manifest.profile_id

    @classmethod
    def load(cls, folder: str | Path, *, profile=None, bin_count: int = PEAK_BIN_COUNT) -> "StemSession":
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise _error(
                "stem.folder_missing",
                f"Stem folder does not exist or is not a directory: {folder}.",
                "Choose the folder containing vocals.wav, drums.wav, bass.wav, and other.wav.",
            )
        if isinstance(bin_count, bool) or not isinstance(bin_count, int) or bin_count <= 0:
            raise _error(
                "stem.invalid_peak_bins",
                "Peak envelope bin count must be a positive integer.",
                f"Use the default {PEAK_BIN_COUNT}-bin waveform envelope.",
            )

        names, absent, profile_id = cls._layout(folder, profile)
        paths = tuple(folder / name for name in names)
        missing = next((path for path in paths if not path.is_file()), None)
        if missing is not None:
            raise _error(
                "stem.missing",
                f"Required stem is missing: {missing.name}.",
                f"Select a complete result folder containing all {len(names)} required WAV files.",
            )

        expected_identity = None
        envelopes = []
        for path in paths:
            try:
                with sf.SoundFile(path) as reader:
                    identity = (reader.samplerate, reader.channels, len(reader))
                    if identity[2] <= 0:
                        raise _error(
                            "stem.empty",
                            f"Required stem is empty: {path.name}.",
                            "Regenerate the stem and select a non-empty Demucs result folder.",
                        )
                    if expected_identity is None:
                        expected_identity = identity
                    else:
                        expected_rate, expected_channels, expected_frames = expected_identity
                        if identity[0] != expected_rate:
                            raise _error(
                                "stem.sample_rate_mismatch",
                                f"{path.name} uses {identity[0]} Hz; expected {expected_rate} Hz.",
                                "Regenerate or align all four stems to the same sample rate.",
                            )
                        if identity[1] != expected_channels:
                            raise _error(
                                "stem.channel_count_mismatch",
                                f"{path.name} has {identity[1]} channels; expected {expected_channels}.",
                                "Regenerate all stems with the same channel layout.",
                            )
                        if identity[2] != expected_frames:
                            raise _error(
                                "stem.frame_count_mismatch",
                                f"{path.name} has {identity[2]} frames; expected {expected_frames}.",
                                "Regenerate or deterministically align all four stems to the same duration.",
                            )
                    envelopes.append(_peak_envelope(reader, identity[2], bin_count))
            except StemSessionError:
                raise
            except Exception as exc:
                raise _error(
                    "stem.unreadable",
                    f"Could not read {path.name}: {exc}.",
                    "Replace the corrupt stem with a readable WAV and load the complete result folder again.",
                ) from exc

        assert expected_identity is not None
        sample_rate, channels, frame_count = expected_identity
        return cls(
            folder,
            paths,
            sample_rate,
            channels,
            frame_count,
            tuple(envelopes),
            names,
            absent,
            profile_id,
        )

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate

    def peaks_for(self, stem_name: str) -> tuple[float, ...]:
        try:
            index = self.names.index(stem_name)
        except ValueError as exc:
            raise KeyError(stem_name) from exc
        return self.peaks[index]

    def is_absent(self, stem_name: str) -> bool:
        """Report whether a published lane was declared absent for this track.

        An absent lane is real, aligned silence: it plays and exports like any
        other lane, and the caller labels it rather than hiding it.
        """
        return Path(stem_name).stem in self.absent
